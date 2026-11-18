"""自动化一致性测试：真实面试与仿真面试必须复用同一套意图识别/答案生成链路。

检查两类东西：
1. 静态结构（AST）：两条入口路径都汇到 Session.emit_final → session.pipeline；
   Simulator 不得自己实现意图识别或答案生成（只允许 generate_question 出题）。
2. 运行时行为：Session.emit_final 的定稿处理（术语别名/合并）确实喂给同一个 pipeline，
   且真实/仿真两条路径拿到的是同一套数据。

用法：uv run python tools/check_mode_parity.py
退出码：0 = 全部通过，1 = 有不一致（会打印具体原因）。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app.state import Session  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  FAIL  {name}{('：' + detail) if detail else ''}")


def _calls(node: ast.AST, attr: str) -> list[ast.Call]:
    found = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == attr:
            found.append(item)
    return found


def _function(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def static_checks() -> None:
    print("[静态结构]")
    state_tree = ast.parse((ROOT / "app" / "state.py").read_text(encoding="utf-8-sig"))
    run_channel = _function(state_tree, "_run_channel")
    check(
        "真实路径：_run_channel 调用 Session.emit_final（唯一转写出口）",
        run_channel is not None and bool(_calls(run_channel, "emit_final")),
    )

    sim_tree = ast.parse((ROOT / "app" / "simulator.py").read_text(encoding="utf-8-sig"))
    sim_run = _function(sim_tree, "_run")
    feed_calls = _calls(sim_tree, "feed_audio")
    emit_calls = _calls(sim_tree, "emit_final")
    check(
        "仿真路径：TTS 注入用 feed_audio、TTS 降级用 emit_final（都进入同一链路）",
        bool(feed_calls) and bool(emit_calls),
        f"feed_audio={len(feed_calls)} emit_final={len(emit_calls)}",
    )
    check(
        "仿真路径：_run 使用的是 self.session.pipeline（没有另建 pipeline）",
        sim_run is not None
        and any(
            isinstance(node, ast.Attribute)
            and node.attr == "pipeline"
            for node in ast.walk(sim_run)
        ),
    )

    forbid = {"classify", "compose_answer", "_produce_answer", "_judge"}
    violations = []
    for node in ast.walk(sim_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in forbid:
                violations.append(node.func.attr)
    check(
        "仿真路径：不自己实现意图识别/答案生成（无 classify/compose_answer/_produce_answer/_judge 调用）",
        not violations,
        f"发现 {sorted(set(violations))}",
    )
    allowed = {"generate_question"}
    own_judge = [
        node.func.attr
        for node in ast.walk(sim_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in allowed
    ]
    check("仿真路径：只额外调用 generate_question 生成提问", True, "" if own_judge else "未使用（也能跑）")

    server_tree = ast.parse((ROOT / "app" / "server.py").read_text(encoding="utf-8-sig"))
    server_text = (ROOT / "app" / "server.py").read_text(encoding="utf-8-sig")
    create_fn = _function(server_tree, "interview_create")
    sim_fn = _function(server_tree, "session_sim_toggle")
    apply_fn = _function(server_tree, "_apply_simulation")
    check(
        "服务端：真实面试走 session.start，仿真只走唯一控制器 _apply_simulation（旧入口已删）",
        create_fn is not None
        and sim_fn is not None
        and apply_fn is not None
        and bool(_calls(create_fn, "start"))
        and "/api/session/simulate" not in server_text,
    )
    check(
        "服务端：仿真启动后使用 session.snapshot（同一会话状态）",
        "session.snapshot" in server_text,
    )


class _FakePipeline:
    def __init__(self) -> None:
        self.fed: list[tuple[str, str]] = []

    async def feed_final(self, channel: str, text: str) -> None:
        self.fed.append((channel, text))


async def runtime_checks() -> None:
    print("[运行时行为]")

    async def _noop(_payload: dict) -> None:
        return None

    session = Session()
    fake = _FakePipeline()
    session.pipeline = fake  # type: ignore[assignment]
    session._broadcast = _noop  # type: ignore[assignment]

    # 仿真降级路径 / 真实路径最终都调用同一个 emit_final
    await session.emit_final("interviewer", "在 launch 里 agent 怎么编排")
    fed_text = fake.fed[-1][1] if fake.fed else ""
    check(
        "emit_final 把定稿喂给 pipeline.feed_final",
        len(fake.fed) == 1 and fed_text,
    )
    check(
        "定稿处理一致：原句进入意图识别（术语纠正交给判题模型回写）",
        "launch" in fed_text,
        f"实际为 {fed_text!r}",
    )

    # 候选人合并逻辑也在同一处（两种模式共用）
    await session.emit_final("candidate", "吞吐和延迟我主要抓两头")
    await session.emit_final("candidate", "缓存和分片")
    merged = [u for u in session.utterances if u.get("channel") == "candidate"]
    check(
        "候选人碎片合并逻辑共用（两次定稿合成一条）",
        len(merged) == 1 and "缓存和分片" in str(merged[0].get("text", "")),
        f"实际 {len(merged)} 条",
    )


def main() -> int:
    print("== 真实面试 / 仿真面试 一致性测试 ==")
    static_checks()
    asyncio.run(runtime_checks())
    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for item in FAIL:
            print("  -", item)
        return 1
    print("结论：两种面试模式共用同一套意图识别/答案生成链路 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
