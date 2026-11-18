"""一键自动化测试（按资源消耗分档，默认只跑便宜且高价值的检查）。

默认（core，约 20 秒，不花大模型 token、不占音频设备）：
  1. 接口面自检          tools/full_test.py --fast（需要本地服务）
  2. 面试接口自检        tools/check_interview_api.py（离线）
  3. 面试状态机自检      tools/check_interviews.py（离线）
  4. 真实/仿真一致性     tools/check_mode_parity.py（离线）
  5. 题库检索评测        tools/eval_retrieval.py（离线）

可选（--models，会真实调用大模型，按量计费）：
  6. 意图识别评测        tools/eval_judge.py（48 用例）
  7. 检索改写评测        tools/eval_search_queries.py --repeat 1（12 用例 × 1 轮）

可选（--smoke，会占用音频设备 + 大模型，约 2 分钟）：
  8. 真实/仿真模式冒烟   tools/smoke_real_mode.py（真实面试 + 仿真输入开关 + 评分）

用法：
  uv run python tools/test_all.py            # 默认：便宜检查
  uv run python tools/test_all.py --models   # 追加两项大模型评测
  uv run python tools/test_all.py --smoke    # 追加真实/仿真冒烟
  uv run python tools/test_all.py --full     # 全部（约 3 分钟 + token 费用）

退出码：0 = 执行项全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PY = sys.executable
BASE = "http://127.0.0.1:8765"

COUNT_RE = re.compile(r"通过\s*(\d+)\s*/\s*失败\s*(\d+)")
METRIC_HINTS = ("准确率", "Recall@", "语义相似度", "追问识别率", "反问识别率", "误报")


class Step:
    def __init__(
        self,
        key: str,
        title: str,
        script: str,
        args: list[str] | None = None,
        need_server: bool = False,
    ):
        self.key = key
        self.title = title
        self.script = script
        self.args = args or []
        self.need_server = need_server


CORE_STEPS = [
    Step("api", "接口面自检", "tools/full_test.py", args=["--fast"], need_server=True),
    Step("api_check", "面试接口自检", "tools/check_interview_api.py"),
    Step("store_check", "面试状态机自检", "tools/check_interviews.py"),
    Step("parity", "真实/仿真一致性", "tools/check_mode_parity.py"),
    Step("retrieval", "题库检索评测", "tools/eval_retrieval.py"),
]

MODEL_STEPS = [
    Step("judge_eval", "意图识别评测（48 用例）", "tools/eval_judge.py"),
    Step(
        "search_eval",
        "检索改写评测（12 用例）",
        "tools/eval_search_queries.py",
        args=["--repeat", "1"],
    ),
]

SMOKE_STEPS = [
    Step("smoke", "真实/仿真模式冒烟", "tools/smoke_real_mode.py", need_server=True),
]


def server_online() -> bool:
    try:
        import httpx

        return httpx.get(f"{BASE}/api/health", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def run_step(step: Step) -> dict:
    path = ROOT / step.script
    if not path.exists():
        return {"ok": False, "status": "MISSING", "seconds": 0.0, "counts": None, "metrics": [], "code": None}

    started = time.perf_counter()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    lines: list[str] = []

    proc = subprocess.Popen(
        [PY, str(path), *step.args],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        lines.append(line.rstrip("\n"))
    code = proc.wait()
    seconds = time.perf_counter() - started

    output = "\n".join(lines)
    match = COUNT_RE.search(output)
    counts = (int(match.group(1)), int(match.group(2))) if match else None
    seen: set[str] = set()
    metrics: list[str] = []
    for line in lines:
        text = line.strip()
        if (
            any(hint in text for hint in METRIC_HINTS)
            and " INFO " not in text
            and "|" not in text
            and text not in seen
        ):
            seen.add(text)
            metrics.append(text)
        if len(metrics) >= 6:
            break
    status = "PASS" if code == 0 else "FAIL"
    return {"ok": code == 0, "status": status, "seconds": seconds, "counts": counts, "metrics": metrics, "code": code}


def main() -> int:
    parser = argparse.ArgumentParser(description="一键自动化测试（默认只跑便宜检查）")
    parser.add_argument("--models", action="store_true", help="追加需要大模型 token 的评测")
    parser.add_argument("--smoke", action="store_true", help="追加需要音频设备的真实/仿真冒烟")
    parser.add_argument("--full", action="store_true", help="全部：core + models + smoke")
    args = parser.parse_args()

    steps = list(CORE_STEPS)
    if args.models or args.full:
        steps += MODEL_STEPS
    if args.smoke or args.full:
        steps += SMOKE_STEPS

    online = server_online()
    print("=" * 68)
    print("面试陪练 · 自动化测试")
    print(f"项目目录: {ROOT}")
    print(f"Python:   {PY}")
    print(f"本地服务: {'在线' if online else '未连接'}")
    print(f"测试分档: {'full' if args.full else ' + '.join(['core'] + (['models'] if args.models else []) + (['smoke'] if args.smoke else []))}")
    print("=" * 68)

    results: list[tuple[Step, dict]] = []
    skipped: list[Step] = []
    total_start = time.perf_counter()

    for step in steps:
        if step.need_server and not online:
            print(f"\n--- 跳过 {step.title}（服务未启动）---")
            skipped.append(step)
            continue
        print(f"\n--- {step.title}  [{step.script} {' '.join(step.args)}] ---")
        results.append((step, run_step(step)))

    total_seconds = time.perf_counter() - total_start
    failed = [(step, res) for step, res in results if not res["ok"]]

    print("\n" + "=" * 68)
    print("汇总")
    print("-" * 68)
    for step, res in results:
        counts = f"  {res['counts'][0]}通过/{res['counts'][1]}失败" if res["counts"] else ""
        print(f"  [{res['status']:4}] {step.title:<22} {res['seconds']:6.1f}s{counts}")
    for step in skipped:
        print(f"  [SKIP] {step.title:<22}（服务未启动）")
    print("-" * 68)
    print(f"  耗时合计: {total_seconds:.1f}s")
    print("=" * 68)

    if failed:
        print("\n失败项：")
        for step, res in failed:
            print(f"  - {step.title}（{step.script}，退出码 {res.get('code')}）")
        return 1

    print("\n全部通过 ✅" if not skipped else "\n执行项全部通过 ✅（有跳过项）")
    for step, res in results:
        for metric in res["metrics"]:
            print(f"  · {step.title}：{metric}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
