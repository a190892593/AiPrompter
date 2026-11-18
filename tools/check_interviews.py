"""面试记录状态机自检：用临时数据目录，不碰真实数据。

覆盖：新建 → 进行中 → 手动停止；以及 2 小时后自动中断；未结束可继续。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="interview-test-"))
os.environ["DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

from app import interviews  # noqa: E402

cfg = {"role": "RAG 算法工程师", "level": "mid", "resume": "三年 RAG 经验", "engine": "local"}

# 1. 新建 = 进行中，开始计时
doc = interviews.create(cfg, position_id=1, position_name="RAG 算法工程师", resume=cfg["resume"], mode="real")
assert doc["status"] == interviews.STATUS_RUNNING, doc
assert interviews.elapsed(doc) < 5
print("PASS 新建面试 = 进行中，开始计时")

# 2. 未结束的可以被「继续」找到
running = interviews.running(5)
assert any(item["id"] == doc["id"] for item in running), running
print("PASS 未结束的面试可以被重新进入")

# 3. 手动停止 = 已结束
loaded = interviews.load(doc["id"])
interviews.finish(loaded)
loaded["report"] = {"overall_score": 88}
interviews.store.save(loaded)
again = interviews.load(doc["id"])
assert again["status"] == interviews.STATUS_FINISHED, again
assert again["ended_at"], again
assert interviews.running(5) == []
print("PASS 手动停止 = 已结束，不再出现在「进行中」")

# 4. 超过 2 小时 → 系统自动中断
old = interviews.create(cfg, position_name="超时岗位", resume="x", mode="real")
old["started_at"] = time.time() - interviews.AUTO_INTERRUPT_SECONDS - 60
interviews.store.save(old)
expired = interviews.load(old["id"])
assert expired["status"] == interviews.STATUS_INTERRUPTED, expired
assert expired["end_reason"].startswith("超过 2 小时"), expired
assert abs(expired["ended_at"] - (expired["started_at"] + interviews.AUTO_INTERRUPT_SECONDS)) < 2
print("PASS 超过 2 小时 = 系统自动中断（结束时间卡在 2 小时整）")

# 5. 自动中断的不能继续
print("    状态列表:", [(item["status"], item["status_label"]) for item in interviews.list_all()])

# 6. 老记录（没有 status 字段）能正常补全
legacy = {"id": "legacy000001", "created_at": "2026-01-01T00:00:00+00:00", "config": {"role": "老岗位"}, "utterances": [], "questions": []}
interviews.store.save(legacy)
fixed = interviews.load("legacy000001")
assert fixed["status"] == interviews.STATUS_FINISHED, fixed
print("PASS 老记录补全 status =", fixed["status"])

# 7. 服务重启后遗留的「进行中」→ 已中断（可继续），计时停在最后一次活动
zombie = interviews.create(cfg, position_name="被杀掉的场次", resume="x", mode="real")
zombie["utterances"] = [
    {"id": "u1", "channel": "interviewer", "text": "问题一", "ts": zombie["started_at"] + 30},
]
interviews.store.save(zombie)
count = interviews.reconcile_orphans()
assert count >= 1, count
recovered = interviews.load(zombie["id"])
assert recovered["status"] == interviews.STATUS_ORPHANED, recovered
assert abs(recovered["ended_at"] - (zombie["started_at"] + 30)) < 1.5, recovered
assert 25 <= interviews.elapsed(recovered) <= 35, interviews.elapsed(recovered)
assert interviews.resume_ready(recovered)
print("PASS 重启后的遗留场次 = 已中断（可继续），用时定格在最后一次活动")

again_open = [item["id"] for item in interviews.open_interviews(10)]
assert zombie["id"] in again_open
print("PASS 「可继续」列表里能找到这场中断的面试")

interviews.reopen(recovered)
assert recovered["status"] == interviews.STATUS_RUNNING
# 继续之后：之前积累的 30 秒还在，停机的那段时间不算进去
assert 27 <= interviews.elapsed(recovered) <= 33, interviews.elapsed(recovered)
print("PASS 继续后计时从上次进度接着走（不把停机时间算进去）")

print("全部通过，数据目录:", TMP)
