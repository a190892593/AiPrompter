"""仿真面试：TTS 合成面试官提问 → 注入面试官通道 → ASR/意图识别 → 把生成的话术 TTS 后注入麦克风通道。

全程不需要真人、不需要物理播放，但走的是同一条 采集队列 → 火山 ASR → 意图识别 → LLM 的链路。

用法：python tools/simulate.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

# Windows 控制台默认 GBK，打印 ✓ / ✗ 这类符号会崩，统一转成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/ws/events"

QUESTIONS = [
    "你们之前做的那个订单系统，技术架构是怎么实现的？",
    "如果流量突然上涨十倍，你们这套架构还扛得住吗？",
    "支付回调的幂等性，你们是怎么保证的？",
]


def load_questions(path: str, limit: int = 0) -> list[str]:
    """从题库文件读问题：一行一条，# 开头是注释。"""
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines[:limit] if limit else lines


def parse_args() -> dict:
    opts = {"engine": "local", "questions": "", "limit": 10, "no_echo": False, "role": "RAG 算法工程师"}
    for arg in sys.argv[1:]:
        if arg.startswith("--engine="):
            opts["engine"] = arg.split("=", 1)[1]
        elif arg.startswith("--questions="):
            opts["questions"] = arg.split("=", 1)[1]
        elif arg.startswith("--limit="):
            opts["limit"] = int(arg.split("=", 1)[1])
        elif arg.startswith("--role="):
            opts["role"] = arg.split("=", 1)[1]
        elif arg == "--no-echo":
            opts["no_echo"] = True
    return opts

state: dict = {"questions": [], "done": set(), "answers": {}, "finals": []}


async def reader(ws) -> None:
    async for raw in ws:
        event = json.loads(raw)
        kind = event.get("type")

        if kind == "final":
            who = "面试官" if event["channel"] == "interviewer" else "我"
            state["finals"].append((who, event["text"]))
            print(f"  ✔ {who}定稿: {event['text']}")

        elif kind == "question":
            state["questions"].append(event["id"])
            state["answers"].setdefault(event["id"], "")
            print(f"  ❓ 意图识别: {event['summary']}  (置信 {event['confidence']}, 合并 {event.get('spans')} 句)")

        elif kind == "answer_delta":
            state["answers"][event["id"]] = state["answers"].get(event["id"], "") + event["text"]

        elif kind == "answer_done":
            state["done"].add(event["id"])
            text = state["answers"].get(event["id"], "")
            print(f"  ✅ 话术完成: {text[:56]}…  ({len(text)} 字)")

        elif kind == "aside":
            print(f"  ⏸ 判为离场/噪音: {event['text'][:30]}")


async def wait_until(predicate, timeout: float, label: str) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.2)
    print(f"  ⚠️ 等待「{label}」超时({timeout:.0f}s)")
    return False


async def wait_quiet(seconds: float = 2.5, timeout: float = 40) -> None:
    """等转写安静下来，避免下一步和还没说完的话重叠。"""
    started = time.time()
    last_count = len(state["finals"])
    quiet_since = time.time()
    while time.time() - started < timeout:
        await asyncio.sleep(0.4)
        if len(state["finals"]) != last_count:
            last_count = len(state["finals"])
            quiet_since = time.time()
        elif time.time() - quiet_since >= seconds:
            return


async def main() -> None:
    opts = parse_args()
    engine = opts["engine"]
    questions = (
        load_questions(opts["questions"], opts["limit"]) if opts["questions"] else QUESTIONS
    )
    print(f"引擎: {engine} | 题目: {len(questions)} 道")

    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        await client.post("/api/devices/monitor/stop")
        await client.post("/api/session/stop")

        resp = await client.post(
            "/api/session/start",
            json={"engine": engine, "role": opts["role"], "answer_level": "standard"},
        )
        resp.raise_for_status()
        print("会话已启动:", resp.json()["devices"], "| 引擎:", resp.json()["engine"])

        async with websockets.connect(WS, max_size=None) as ws:
            task = asyncio.create_task(reader(ws))
            await asyncio.sleep(1.2)

            for index, question in enumerate(questions, start=1):
                print(f"\n=== 第 {index} 问（模拟面试官说话）===")
                known = set(state["questions"])
                r = await client.post(
                    "/api/session/inject_audio", json={"channel": "interviewer", "text": question}
                )
                print(f"  注入 {r.json()['seconds']}s 音频")

                new_question = await wait_until(
                    lambda: len(set(state["questions"]) - known) > 0, 45, "判出问题"
                )
                if not new_question:
                    continue

                qid = [q for q in state["questions"] if q not in known][0]
                await wait_until(lambda: qid in state["done"], 45, "生成话术")

                answer = state["answers"].get(qid, "").strip()
                if not answer or opts["no_echo"]:
                    continue

                print("=== 模拟我照念话术 ===")
                r = await client.post(
                    "/api/session/inject_audio", json={"channel": "candidate", "text": answer}
                )
                print(f"  注入 {r.json()['seconds']}s 音频")
                await wait_quiet()

            report = (await client.post("/api/session/finish")).json()
            print("\n=== 评分 ===")
            print(f"综合: {report.get('overall_score')} | {report.get('verdict')}")
            for item in report.get("per_question", []):
                print(f"  第{item['index']}题 {item['score']}分: {item['comment']}")
            task.cancel()

        await client.post("/api/session/stop")


if __name__ == "__main__":
    asyncio.run(main())
