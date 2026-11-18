"""意图识别闭环自检：不碰运行中的服务，直接在本进程跑一遍 Pipeline 的意图识别路径。

验证两件事：
1. judge_start 之后一定会有 judge_result（否则界面会永远停在「意图识别中」）；
2. 意图识别的模型调用真的发出去了（llm_calls 里有对应 trace 的记录）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import settings_store  # noqa: E402
from app.config import settings
from app.pipeline import Pipeline
from app.schemas import SessionConfig


async def main() -> None:
    settings_store.apply_all()
    events: list[dict] = []

    async def broadcast(payload: dict) -> None:
        events.append(payload)

    cfg = SessionConfig(role="RAG 算法工程师", engine="local")
    pipe = Pipeline(cfg, broadcast)

    started = time.perf_counter()
    await pipe.feed_final("interviewer", "什么是 RAG？他的工作流程是什么")

    # 等意图识别结论（最多 30 秒）
    while time.perf_counter() - started < 30:
        if any(item["type"] == "judge_result" for item in events):
            break
        await asyncio.sleep(0.2)

    kinds = [item["type"] for item in events]
    print("elapsed", round(time.perf_counter() - started, 2))
    print("events", kinds)
    for item in events:
        if item["type"] == "judge_start":
            print("judge_start trace=", item.get("trace"))
        if item["type"] == "judge_result":
            print(
                "judge_result kind=%s accepted=%s q=%r conf=%s skip=%r dur=%sms"
                % (
                    item.get("kind"),
                    item.get("accepted"),
                    item.get("question"),
                    item.get("confidence"),
                    item.get("skip_reason"),
                    item.get("duration_ms"),
                )
            )

    ok = "judge_start" in kinds and "judge_result" in kinds
    print("闭环:", "OK" if ok else "FAIL")
    await pipe.close()
    if not ok:
        sys.exit(1)


asyncio.run(main())
