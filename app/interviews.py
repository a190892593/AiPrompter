"""一场面试 = 一条记录。

规则（产品约定）：
- 点「新建面试」必须选岗位、填个人简历，确认后才建记录，**从这一刻开始计时**；
- 只有手动点「停止面试」才算正常结束；
- 没有手动停止、且已超过 2 小时的，一律标记为「系统自动中断」；
- 未结束的面试可以重新进入，之前的对话和提词都还在。

记录本身还是 store 里的一份 JSON 文档（一次面试一个 doc），
这里只负责状态机与超时判定，不碰音频。
"""

from __future__ import annotations

import logging
import time
import secrets
from datetime import datetime, timezone
from typing import Any

from . import llmlog, store

log = logging.getLogger("interviews")

# 超过这么久没手动结束，判定为系统自动中断
AUTO_INTERRUPT_SECONDS = 2 * 3600
# 「已中断」的场次闲置这么久还没回来继续，就当已结束归档，别一直提示
ARCHIVE_AFTER_SECONDS = 12 * 3600

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_INTERRUPTED = "auto_interrupted"
# 进程被杀/重启导致没走完停止流程的场次：不算「进行中」（没有采集在跑），但还能继续
STATUS_ORPHANED = "interrupted"

STATUS_LABEL = {
    STATUS_RUNNING: "进行中",
    STATUS_FINISHED: "已结束",
    STATUS_INTERRUPTED: "系统自动中断",
    STATUS_ORPHANED: "已中断（可继续）",
}

# 这两种状态都允许「重新进入」
RESUMABLE = {STATUS_RUNNING, STATUS_ORPHANED}

END_REASON_MANUAL = "手动停止"
END_REASON_AUTO = "超过 2 小时未手动结束，系统自动中断"
END_REASON_ORPHANED = "服务重启/异常退出，未手动停止"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    """面试ID：年月日时分 + 4 位随机数，例如 202611181542-6859。

    时间前缀让列表天然按创建顺序排序，随机后缀避免同一秒内重复。
    """
    while True:
        candidate = f"{datetime.now().strftime('%Y%m%d%H%M')}-{secrets.randbelow(10000):04d}"
        if store.get(candidate) is None:
            return candidate


def create(
    config: dict[str, Any],
    *,
    position_id: int | None = None,
    position_name: str = "",
    resume: str = "",
    mode: str = "real",
    devices: dict[str, str] | None = None,
    engine: str = "",
) -> dict[str, Any]:
    """建一条「进行中」的面试记录，并立即落库（这样刷新/重启也不会丢）。"""
    started = time.time()
    doc: dict[str, Any] = {
        "id": new_id(),
        "created_at": _iso(started),
        "started_at": started,
        "ended_at": None,
        "status": STATUS_RUNNING,
        "end_reason": "",
        "mode": mode,
        "position_id": position_id,
        "position_name": position_name,
        "resume": resume,
        "config": config,
        "devices": devices or {"interviewer": "", "candidate": ""},
        "engine": engine,
        "utterances": [],
        "questions": [],
        "report": None,
        "active": False,
    }
    store.save(doc)
    log.info(
        "新建面试: id=%s 岗位=%r 级别=%s 模式=%s 简历=%d字",
        doc["id"],
        config.get("role"),
        config.get("level"),
        mode,
        len(resume),
    )
    return doc


def _legacy_status(doc: dict[str, Any]) -> str:
    """老记录没有 status 字段：有报告=已结束，仍在跑=进行中，其余=已结束。"""
    if doc.get("status"):
        return str(doc["status"])
    if doc.get("report"):
        return STATUS_FINISHED
    return STATUS_RUNNING if doc.get("active") else STATUS_FINISHED


def normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """补齐老数据缺的字段，返回**可能被就地修改过**的 doc。"""
    if not doc.get("started_at"):
        created = doc.get("created_at") or ""
        try:
            doc["started_at"] = datetime.fromisoformat(created).timestamp()
        except (TypeError, ValueError):
            doc["started_at"] = time.time()
    doc["status"] = _legacy_status(doc)
    doc.setdefault("ended_at", None)
    doc.setdefault("end_reason", "")
    doc.setdefault("position_name", (doc.get("config") or {}).get("role", ""))
    doc.setdefault("resume", (doc.get("config") or {}).get("resume", ""))
    return doc


def expire(doc: dict[str, Any], *, now: float | None = None) -> bool:
    """超过 2 小时还没结束 → 标记系统自动中断。返回是否发生了变更。"""
    current = time.time() if now is None else now
    started = float(doc.get("started_at") or 0.0)
    if not started or doc.get("status") != STATUS_RUNNING:
        return False
    if current - started <= AUTO_INTERRUPT_SECONDS:
        return False
    doc["status"] = STATUS_INTERRUPTED
    doc["ended_at"] = started + AUTO_INTERRUPT_SECONDS
    doc["end_reason"] = END_REASON_AUTO
    doc["active"] = False
    log.warning(
        "面试超时自动中断: id=%s 开始于 %s（已 %.1f 小时）",
        doc.get("id"),
        _iso(started),
        (current - started) / 3600,
    )
    return True


def finish(doc: dict[str, Any], *, reason: str = END_REASON_MANUAL) -> dict[str, Any]:
    """手动结束：记结束时间。已经是自动中断的不要覆盖。"""
    if doc.get("status") in (STATUS_RUNNING, STATUS_ORPHANED):
        if not doc.get("ended_at"):
            doc["ended_at"] = time.time()
        doc["status"] = STATUS_FINISHED
        doc["end_reason"] = reason
    doc["active"] = False
    return doc


def elapsed(doc: dict[str, Any], *, now: float | None = None) -> int:
    """本场已进行多少秒：结束的按 ended_at 算，进行中的算到此刻。"""
    current = time.time() if now is None else now
    started = float(doc.get("started_at") or 0.0)
    if not started:
        return 0
    ended = doc.get("ended_at")
    if ended:
        return max(0, int(float(ended) - started))
    return max(0, int(current - started))


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    """列表用：不带转写与卡片大字段。"""
    questions = doc.get("questions") or []
    report = doc.get("report") or {}
    status = doc.get("status") or STATUS_FINISHED
    started = float(doc.get("started_at") or 0)
    usage = doc.get("usage")
    if not usage and started:
        end = float(doc.get("ended_at") or time.time())
        usage = llmlog.usage_between(started, end)
    engines = doc.get("engines") or ([doc["engine"]] if doc.get("engine") else [])
    return {
        "id": doc.get("id"),
        "role": doc.get("position_name") or (doc.get("config") or {}).get("role", ""),
        "position_id": doc.get("position_id"),
        "mode": doc.get("mode", "real"),
        "sim_mode": doc.get("sim_mode", ""),
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
        "created_at": doc.get("created_at"),
        "started_at": doc.get("started_at"),
        "ended_at": doc.get("ended_at"),
        "elapsed": elapsed(doc),
        "turns": len(questions),
        "utterances": len(doc.get("utterances") or []),
        "score": report.get("overall_score"),
        "verdict": report.get("verdict", ""),
        "engine": doc.get("engine", ""),
        "engines": engines,
        "usage": usage,
        "end_reason": doc.get("end_reason", ""),
    }


def load(interview_id: str) -> dict[str, Any] | None:
    doc = store.get(interview_id)
    if doc is None:
        return None
    before = dict(doc)
    normalize(doc)
    if expire(doc):
        store.save(doc)
    elif doc != before:
        store.save(doc)
    return doc


def last_activity(doc: dict[str, Any]) -> float:
    """最后一次「真的在面试」的时刻：转写/卡片的最新时间戳。"""
    stamps = [float(u.get("ts") or 0) for u in (doc.get("utterances") or [])]
    stamps += [float(q.get("ts") or 0) for q in (doc.get("questions") or [])]
    stamps = [stamp for stamp in stamps if stamp > 0]
    return max(stamps) if stamps else float(doc.get("started_at") or time.time())


def orphan(doc: dict[str, Any]) -> bool:
    """进程重启后发现的「进行中」记录：没有采集在跑，改成「已中断（可继续）」。

    结束时间取最后一次真实活动时间（而不是此刻），这样计时不会一直涨。
    """
    if doc.get("status") != STATUS_RUNNING:
        return False
    ended = last_activity(doc)
    doc["status"] = STATUS_ORPHANED
    doc["ended_at"] = ended
    doc["end_reason"] = END_REASON_ORPHANED
    doc["active"] = False
    log.info(
        "面试中断（服务重启）: id=%s 最后活动=%s 已进行 %ss",
        doc.get("id"),
        _iso(ended),
        elapsed(doc),
    )
    return True


def resume_ready(doc: dict[str, Any]) -> bool:
    return str(doc.get("status") or "") in RESUMABLE


def reopen(doc: dict[str, Any]) -> dict[str, Any]:
    """重新进入：状态回到进行中，开始时间前移，让计时从上次的进度接着走。"""
    done = elapsed(doc)
    started = time.time() - done
    doc["status"] = STATUS_RUNNING
    doc["started_at"] = started
    doc["ended_at"] = None
    doc["end_reason"] = ""
    doc["active"] = True
    return doc


def reconcile_orphans() -> int:
    """启动时调用：把上次进程留下的「进行中」标成已中断（仍可继续）。

    同时给「中断后很久没回来」的场次收尾：超过 2 小时没继续的，当成已结束归档，
    这样提词器不会一直提示一场早就过去的面试。
    """
    count = 0
    now = time.time()
    for doc in store.latest(1000):
        normalize(doc)
        changed = expire(doc) or orphan(doc)
        if doc.get("status") == STATUS_ORPHANED:
            ended = float(doc.get("ended_at") or 0.0)
            if ended and now - ended > ARCHIVE_AFTER_SECONDS:
                doc["status"] = STATUS_FINISHED
                doc["end_reason"] = "中断后长时间未继续，已归档"
                doc["active"] = False
                changed = True
        if changed:
            store.save(doc)
            count += 1
    if count:
        log.info("启动巡检: %d 场未正常结束的面试已处理（可继续的标为已中断，过期的归档）", count)
    return count


def open_interviews(limit: int = 1) -> list[dict[str, Any]]:
    """还没结束、可以继续的面试（进行中或已中断），最近的排在前面。"""
    docs = store.latest(200)
    items: list[dict[str, Any]] = []
    for doc in docs:
        normalize(doc)
        if expire(doc):
            store.save(doc)
        if doc.get("status") in RESUMABLE:
            items.append(doc)
    items.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
    return items[:limit]


def running(limit: int = 1) -> list[dict[str, Any]]:
    """真正在进行中的面试（兼容旧调用）。"""
    docs = store.latest(50)
    items: list[dict[str, Any]] = []
    for doc in docs:
        normalize(doc)
        if expire(doc):
            store.save(doc)
        if doc.get("status") == STATUS_RUNNING:
            items.append(doc)
            if len(items) >= limit:
                break
    return items


def list_all(limit: int = 100, status: str = "") -> list[dict[str, Any]]:
    docs = store.latest(limit)
    items = []
    for doc in docs:
        normalize(doc)
        if expire(doc):
            store.save(doc)
        if status and doc.get("status") != status:
            continue
        items.append(summary(doc))
    items.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return items
