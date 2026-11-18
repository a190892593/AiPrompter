"""面试接口自检：假 Session 跑一遍 新建 → 进行中 → 停止 → 不能再继续。

不碰真实音频设备，也不写真实 data 目录（用临时 DATA_DIR）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="interview-api-"))
os.environ["DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

from app import server  # noqa: E402  （ROOT 已加入 sys.path）
from app.schemas import InterviewIn  # noqa: E402


class FakeSession:
    """只保留状态机需要的那几个方法。"""

    def __init__(self) -> None:
        self.active = False
        self.session_id = ""
        self.meta: dict = {}
        self.utterances: list = []
        self.pipeline = None
        self.started_at = 0.0

    async def start(self, cfg, broadcast, interview=None):  # noqa: ANN001
        self.active = True
        self.session_id = interview["id"]
        self.started_at = interview["started_at"]
        self.meta = {
            "status": "running",
            "started_at": interview["started_at"],
            "position_name": interview["position_name"],
            "mode": interview["mode"],
            "resume": interview["resume"],
        }
        return self.snapshot()

    async def stop(self):  # noqa: ANN201
        if self.meta.get("status") == "running":
            self.meta.update({"status": "finished", "end_reason": "手动停止"})
        self.active = False
        return self.snapshot()

    async def finish(self):  # noqa: ANN201
        return {"overall_score": 77, "verdict": "自检用", "degraded": True}

    async def _persist(self):  # noqa: ANN201
        from app import store

        store.save(
            {
                "id": self.session_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": {},
                "utterances": [],
                "questions": [],
                **self.meta,
            }
        )

    async def resume_from(self, doc, broadcast):  # noqa: ANN001, ANN201
        self.active = True
        self.session_id = doc["id"]
        self.meta = {k: doc[k] for k in ("status", "started_at", "position_name", "mode", "resume") if k in doc}
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "active": self.active,
            "engine": "local",
            "devices": {"interviewer": "假扬声器", "candidate": "假麦克风"},
            "levels": {"interviewer": 0.0, "candidate": 0.0},
            "dropped": 0,
            "started_at": self.started_at,
            "turns": 0,
            "interview": {"id": self.session_id, **self.meta},
        }


async def main() -> None:
    fake = FakeSession()
    server.session = fake

    body = InterviewIn(
        role="RAG 算法工程师",
        resume="三年 RAG 检索优化经验，QPS 提升 3 倍",
        position_id=3,
        position_name="RAG 算法工程师",
        level="mid",
        engine="local",
        mode="real",
    )
    created = await server.interview_create(body)
    interview = created["interview"]
    assert interview["status"] == "running", interview
    assert interview["role"] == "RAG 算法工程师"
    print("PASS 新建面试接口: id=%s status=%s" % (interview["id"], interview["status"]))

    running = await server.interview_running()
    assert running["item"] and running["item"]["id"] == interview["id"], running
    print("PASS 进行中接口能查到这条")

    stopped = await server.interview_stop(interview["id"])
    assert stopped["interview"]["status"] == "finished", stopped
    assert stopped["report"]["overall_score"] == 77
    print("PASS 停止面试接口: status=%s 报告=%s 分" % (stopped["interview"]["status"], stopped["report"]["overall_score"]))

    again = await server.interview_stop(interview["id"])
    assert again.get("already") is True, again
    print("PASS 重复停止是幂等的")

    try:
        await server.interview_resume(interview["id"])
    except server.HTTPException as exc:
        assert exc.status_code == 409, exc
        print("PASS 已结束的面试不能继续（409）")
    else:
        raise AssertionError("已结束的面试居然还能继续")

    # ---------- 删除 ----------
    from app import interviews as iv

    finished_doc = iv.create({"role": "待删除"}, position_name="待删除", resume="x", mode="simulate")
    iv.finish(finished_doc)
    from app import store as store_mod

    store_mod.save(finished_doc)

    live_doc = iv.create({"role": "进行中"}, position_name="进行中", resume="x", mode="real")

    removed = await server.interview_delete(finished_doc["id"])
    assert removed.get("ok") is True, removed
    assert iv.load(finished_doc["id"]) is None
    print("PASS 单条删除：已结束的记录可删除")

    try:
        await server.interview_delete(live_doc["id"])
    except server.HTTPException as exc:
        assert exc.status_code == 409, exc
        print("PASS 单条删除：进行中的记录被拒绝（409）")
    else:
        raise AssertionError("进行中的记录居然能删")

    pending = iv.create({"role": "批量一"}, position_name="批量一", resume="x", mode="real")
    iv.finish(pending)
    store_mod.save(pending)
    batch = await server.interview_batch_delete(
        server.BatchDeleteIn(ids=[pending["id"], live_doc["id"], "not-exist-id"])
    )
    assert batch["deleted"] == 1, batch
    assert len(batch["skipped"]) == 2, batch
    assert iv.load(pending["id"]) is None
    assert iv.load(live_doc["id"]) is not None
    print("PASS 批量删除：删除 1 条，进行中/不存在各跳过 1 条")

    print("全部通过，数据目录:", TMP)


asyncio.run(main())
