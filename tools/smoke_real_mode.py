"""真实面试模式冒烟测试（无需真人说话）。

用真实面试的启动链路（打开 WASAPI 采集、本地引擎、真实设备）起一场面试，
然后通过 /api/session/inject 注入一句面试官提问，验证：
  采集/引擎/设备 → 转写定稿 → 意图识别 → 出卡 → 三版话术 → 停止 → 评分报告 → 清理

注意：它不能代替真人试音（真实声音进环回/麦克风这条路径需要人说话），
但能证明除了「声音本身」之外的真实面试基础设施和业务链路是通的。

用法：
  uv run python tools/smoke_real_mode.py          # 跑完自动删除测试记录
  uv run python tools/smoke_real_mode.py --keep   # 保留记录，方便页面里看
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8765"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  FAIL  {name}{('：' + detail) if detail else ''}")


def wait_for(fn, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="真实面试模式冒烟测试")
    parser.add_argument("--keep", action="store_true", help="保留测试面试记录")
    parser.add_argument("--skip-simulate", action="store_true", help="跳过仿真模式对照测试")
    args = parser.parse_args()

    client = httpx.Client(base_url=BASE, timeout=180)
    print("== 真实面试模式冒烟测试 ==")

    try:
        health = client.get("/api/health")
        check("服务在线", health.status_code == 200, f"HTTP {health.status_code}")
        if health.status_code != 200:
            return 1

        state = client.get("/api/session/state").json()
        if state.get("active"):
            print("  已有面试在进行中，先停止或等待它结束再跑")
            return 1

        positions = client.get("/api/positions").json()["items"]
        resumes = client.get("/api/resumes").json()["items"]
        check("岗位库可用", bool(positions), "没有岗位")
        check("简历库可用", bool(resumes), "没有简历")
        if not positions or not resumes:
            return 1
        pos = positions[0]
        resume_content = client.get(f"/api/resumes/{resumes[0]['id']}").json()["content"]

        devices = client.get("/api/devices").json()
        outputs = devices.get("outputs") or []
        inputs = devices.get("inputs") or []
        check("检测到环回设备", bool(outputs), "没有可用的扬声器环回")
        check("检测到麦克风", bool(inputs), "没有可用的麦克风")
        if not outputs or not inputs:
            return 1

        started = client.post(
            "/api/interviews",
            json={
                "role": pos["name"],
                "resume": resume_content,
                "position_id": pos["id"],
                "position_name": pos["name"],
                "level": "mid",
                "engine": "local",
                "answer_level": "standard",
                "output_device": "",
                "input_device": "",
                "mode": "real",
                "background": "",
            },
        )
        check("真实面试启动成功", started.status_code == 200, started.text[:120])
        if started.status_code != 200:
            return 1
        data = started.json()
        interview = data.get("interview") or {}
        iid = str(interview.get("id") or "")
        check("拿到面试ID", bool(iid), "响应里没有 id")
        check("会话处于 active", bool(data.get("active")))
        live_now = client.get("/api/session/state").json().get("session_id")
        check("会话ID与面试ID一致", live_now == iid, f"当前会话={live_now} 期望={iid}（可能有并发操作）")
        check("模式为 real", interview.get("mode") == "real", str(interview.get("mode")))
        check("设备已绑定", bool((data.get("devices") or {}).get("interviewer")), str(data.get("devices")))
        check("引擎为本地", "local" in str(interview.get("engines") or data.get("engine_kind") or ""))

        # 用注入模拟「ASR 定稿」；真实声音采集由人试音保证
        inject = client.post(
            "/api/session/inject",
            json={"channel": "interviewer", "text": "你们 RAG 的召回率具体是怎么提上去的？线上口径是什么？"},
        )
        check("注入面试官提问", inject.status_code == 200, inject.text[:120])

        def question_ready():
            doc = client.get(f"/api/interviews/{iid}").json()
            return (doc.get("questions") or [None])[0]

        card = wait_for(question_ready, timeout=60)
        check("意图识别出卡", bool(card), "60 秒内没出卡")
        if card:
            check("问题是自包含的", bool(str(card.get("question") or "").strip()), repr(card.get("question")))
            check(
                "保留了检索改写",
                isinstance(card.get("search_queries"), list) and bool(card.get("search_queries")),
                str(card.get("search_queries")),
            )

        def answer_ready():
            doc = client.get(f"/api/interviews/{iid}").json()
            qs = doc.get("questions") or []
            if not qs:
                return None
            answers = qs[0].get("answers") or {}
            return answers if answers.get("standard") else None

        answers = wait_for(answer_ready, timeout=90)
        check("标准版话术生成", bool(answers), "90 秒内没有标准版答案")
        if answers:
            check("三版话术齐全", bool(answers.get("brief")) and bool(answers.get("deep")), str(list(answers)))
            card_now = (client.get(f"/api/interviews/{iid}").json().get("questions") or [{}])[0]
            check("三版模式字段落盘", isinstance(card_now.get("modes"), dict), str(card_now.get("modes")))
            # 真实面试自动联网 + 深度档思考的规则应与仿真一致
            modes = (client.get(f"/api/interviews/{iid}").json().get("questions") or [{}])[0].get("modes") or {}
            check("标准档自动联网", bool((modes.get("standard") or {}).get("web")), str(modes))
            check("深度档思考+联网", bool((modes.get("deep") or {}).get("thinking")) and bool((modes.get("deep") or {}).get("web")), str(modes))

        stopped = client.post(f"/api/interviews/{iid}/stop")
        check("停止真实面试", stopped.status_code == 200, stopped.text[:120])
        doc = client.get(f"/api/interviews/{iid}").json()
        check("状态变为已结束", (doc.get("status") or "") == "finished", str(doc.get("status")))
        check("生成评分报告", bool(doc.get("report")), "没有 report")
        check("记录可回放（有对话）", bool(doc.get("utterances")), "没有 utterances")

        if not args.keep:
            dropped = client.post("/api/interviews/batch-delete", json={"ids": [iid]})
            check("清理测试记录", dropped.status_code == 200, dropped.text[:120])
        else:
            print(f"  已保留测试记录: /interviews/{iid}")

        if not args.skip_simulate:
            print("\n-- 仿真输入开关（同一个开始按钮，开/关都验证）--")
            sim2 = client.post(
                "/api/interviews",
                json={
                    "role": pos["name"],
                    "resume": resume_content,
                    "position_id": pos["id"],
                    "position_name": pos["name"],
                    "level": "mid",
                    "engine": "local",
                    "answer_level": "standard",
                    "output_device": "",
                    "input_device": "",
                    "mode": "real",
                    "background": "",
                },
            )
            check("统一入口启动成功", sim2.status_code == 200, sim2.text[:120])
            if sim2.status_code == 200:
                sim_iid = str(((sim2.json().get("interview") or {}).get("id")) or "")
                sim_doc = lambda: client.get(f"/api/interviews/{sim_iid}").json()

                toggled = client.post("/api/session/sim", json={"enabled": True, "audible": True, "mode": "resume"})
                check("仿真开关：开启", toggled.status_code == 200, toggled.text[:120])
                if toggled.status_code == 200:
                    check("开关状态写入会话", bool(toggled.json().get("enabled")))
                sim_doc_now = sim_doc()
                check("记录模式切换为 simulate", (sim_doc_now.get("mode") or "") == "simulate", str(sim_doc_now.get("mode")))

                def sim_card_ready():
                    doc = sim_doc()
                    return (doc.get("questions") or [None])[0]

                sim_card = wait_for(sim_card_ready, timeout=150)
                check("仿真：TTS→ASR→出卡", bool(sim_card), "150 秒内没出卡（检查 TTS/ASR）")
                if sim_card:
                    check("仿真：问题自包含", bool(str(sim_card.get("question") or "").strip()))

                def sim_answer_ready():
                    doc = sim_doc()
                    qs = doc.get("questions") or []
                    if not qs:
                        return None
                    answers = qs[0].get("answers") or {}
                    return answers if answers.get("standard") else None

                sim_answers = wait_for(sim_answer_ready, timeout=120)
                check("仿真：标准版话术生成", bool(sim_answers))

                off = client.post("/api/session/sim", json={"enabled": False})
                check("仿真开关：关闭", off.status_code == 200, off.text[:120])
                if off.status_code == 200:
                    check("关闭后会话仍在运行", bool(off.json().get("active")))
                    check("关闭后模式回到 real", ((sim_doc().get("mode") or "") == "real"))
                sim_stop = client.post(f"/api/interviews/{sim_iid}/stop")
                check("仿真对照：停止并评分", sim_stop.status_code == 200)
                sim_final = sim_doc()
                check("仿真对照：状态已结束", (sim_final.get("status") or "") == "finished", str(sim_final.get("status")))
                check("仿真对照：有评分报告", bool(sim_final.get("report")))
                if not args.keep:
                    client.post("/api/interviews/batch-delete", json={"ids": [sim_iid]})

    except httpx.HTTPError as exc:
        check("HTTP 调用", False, str(exc))
    finally:
        client.close()

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for item in FAIL:
            print("  -", item)
        return 1
    print("结论：真实面试模式的基础设施与业务链路可用 ✅（声音采集仍需真人试音确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
