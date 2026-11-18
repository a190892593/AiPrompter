"""LLM 调用日志：把每次调用的出入参、耗时、token、错误都记下来，方便定位问题。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

from .config import settings

log = logging.getLogger("llmlog")

# 当前正在跑/正在收尾的面试 id；一次只允许一场面试，调用日志用它绑定归属。
_current_interview = ""


def set_interview(interview_id: str) -> None:
    """设置/清除当前面试 id；清除传空字符串。"""
    global _current_interview
    _current_interview = (interview_id or "").strip()


def current_interview() -> str:
    return _current_interview

# DeepSeek 官方价（元 / 百万 tokens，2026-11 抓取）：
# deepseek-flash 缓存未命中输入峰时 2 元、谷时 1 元；输出峰时 8 元、谷时 4 元。
# 缓存命中价更低，但日志里没有缓存命中 token，这里按未命中估算（略偏高）。
_CN_TZ = timezone(timedelta(hours=8))


def _is_peak(moment: datetime) -> bool:
    """北京时间周一至周五 9:00-12:00、14:00-18:00 为高峰（法定节假日未排除）。"""
    local = moment.astimezone(_CN_TZ)
    if local.weekday() >= 5:
        return False
    hour = local.hour + local.minute / 60
    return (9 <= hour < 12) or (14 <= hour < 18)


def _price(model: str, moment: datetime) -> tuple[float, float] | None:
    """返回（输入、输出）每百万 tokens 的人民币价格；不认识的模型返回 None。"""
    name = (model or "").lower()
    if "deepseek-flash" in name or "deepseek-chat" in name or "deepseek-v4-flash" in name:
        return (2.0, 8.0) if _is_peak(moment) else (1.0, 4.0)
    return None


def usage_between(start_ts: float, end_ts: float) -> dict[str, Any]:
    """统计某场面试时间窗内的模型调用：模型名、tokens、估算费用（CNY）。"""
    if not start_ts:
        return {
            "models": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "currency": "CNY",
            "priced": True,
        }
    start = datetime.fromtimestamp(start_ts, timezone.utc).isoformat(timespec="seconds")
    end = datetime.fromtimestamp(end_ts or start_ts, timezone.utc).isoformat(timespec="seconds")
    with _lock:
        rows = _db().execute(
            """SELECT created_at, model, prompt_tokens, completion_tokens
               FROM llm_calls
               WHERE created_at >= ? AND created_at <= ? AND status = 'ok'
               ORDER BY id""",
            (start, end),
        ).fetchall()

    models: set[str] = set()
    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    priced = True
    for created_at, model, prompt, completion in rows:
        models.add(str(model))
        prompt_tokens += int(prompt or 0)
        completion_tokens += int(completion or 0)
        try:
            moment = datetime.fromisoformat(str(created_at))
        except (TypeError, ValueError):
            moment = datetime.now(timezone.utc)
        price = _price(str(model), moment)
        if price is None:
            priced = False
            continue
        cost += prompt / 1_000_000 * price[0] + completion / 1_000_000 * price[1]

    return {
        "models": sorted(models),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost": round(cost, 4) if priced else None,
        "currency": "CNY",
        "priced": priced,
    }

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
MAX_TEXT = 20000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_start_utc() -> str:
    """本地今天 0 点对应的 UTC 时间串（sqlite 里存的是 UTC ISO）。
    "今日调用" 要按本地自然日算，不能按 UTC 日期算，否则东八区要到早上 8 点才翻篇。
    """
    local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_calls (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   model TEXT NOT NULL,
                   kind TEXT NOT NULL,
                   status TEXT NOT NULL,
                   latency_ms INTEGER NOT NULL DEFAULT 0,
                   prompt_tokens INTEGER NOT NULL DEFAULT 0,
                   completion_tokens INTEGER NOT NULL DEFAULT 0,
                   total_tokens INTEGER NOT NULL DEFAULT 0,
                   request TEXT NOT NULL DEFAULT '',
                   response TEXT NOT NULL DEFAULT '',
                   error TEXT NOT NULL DEFAULT '',
                   trace TEXT NOT NULL DEFAULT '',
                   interview_id TEXT NOT NULL DEFAULT '',
                   category TEXT NOT NULL DEFAULT ''
               )"""
        )
        self_conn = _conn
        self_conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls(created_at)")
        # 老库补 trace / interview_id 列
        cols = {row[1] for row in self_conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if "trace" not in cols:
            self_conn.execute("ALTER TABLE llm_calls ADD COLUMN trace TEXT NOT NULL DEFAULT ''")
        if "interview_id" not in cols:
            self_conn.execute(
                "ALTER TABLE llm_calls ADD COLUMN interview_id TEXT NOT NULL DEFAULT ''"
            )
        if "category" not in cols:
            self_conn.execute("ALTER TABLE llm_calls ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        self_conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_trace ON llm_calls(trace)")
        self_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_interview ON llm_calls(interview_id)"
        )
        self_conn.commit()
    return _conn


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"\n…（截断，共 {len(text)} 字）"


def record(
    *,
    model: str,
    kind: str,
    status: str,
    latency_ms: int,
    messages: list[dict[str, Any]] | None = None,
    response: str = "",
    usage: dict[str, Any] | None = None,
    error: str = "",
    trace: str = "",
    category: str = "",
) -> None:
    """写一条调用记录。日志失败绝不影响主流程。"""
    if not settings.llm_log_enabled:
        return
    usage = usage or {}
    try:
        request_json = json.dumps(messages or [], ensure_ascii=False)
        with _lock:
            db = _db()
            db.execute(
                """INSERT INTO llm_calls
                   (created_at, model, kind, status, latency_ms, prompt_tokens,
                    completion_tokens, total_tokens, request, response, error, trace, interview_id,
                    category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    model,
                    kind,
                    status,
                    int(latency_ms),
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    _clip(request_json),
                    _clip(response),
                    _clip(error),
                    trace,
                    current_interview(),
                    category,
                ),
            )
            db.commit()
    except Exception:  # noqa: BLE001 - 日志坏了不能让面试中断
        log.exception("写入调用日志失败")


def list_calls(
    limit: int = 50,
    offset: int = 0,
    only_error: bool = False,
    trace: str = "",
    interview_id: str = "",
    category: str = "",
) -> dict[str, Any]:
    clauses = []
    if only_error:
        clauses.append("status != 'ok'")
    if trace:
        clauses.append("trace = ?")
    if interview_id:
        clauses.append("interview_id = ?")
    if category:
        clauses.append("category = ?")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params: list[Any] = []
    if trace:
        params.append(trace)
    if interview_id:
        params.append(interview_id)
    if category:
        params.append(category)
    with _lock:
        db = _db()
        rows = db.execute(
            f"""SELECT id, created_at, model, kind, status, latency_ms,
                       prompt_tokens, completion_tokens, total_tokens, error, trace, interview_id,
                       category
                FROM llm_calls {where}
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM llm_calls {where}", params).fetchone()[0]
    items = [
        {
            "id": r[0],
            "created_at": r[1],
            "model": r[2],
            "kind": r[3],
            "status": r[4],
            "latency_ms": r[5],
            "prompt_tokens": r[6],
            "completion_tokens": r[7],
            "total_tokens": r[8],
            "error": r[9],
            "trace": r[10],
            "interview_id": r[11],
            "category": r[12],
        }
        for r in rows
    ]
    return {"items": items, "total": total}


def delete_call(call_id: int) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute("DELETE FROM llm_calls WHERE id = ?", (call_id,))
        db.commit()
    return cursor.rowcount > 0


def get_call(call_id: int) -> dict[str, Any] | None:
    with _lock:
        row = _db().execute(
            """SELECT id, created_at, model, kind, status, latency_ms, prompt_tokens,
                      completion_tokens, total_tokens, request, response, error, trace, interview_id,
                      category
               FROM llm_calls WHERE id = ?""",
            (call_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "model": row[2],
        "kind": row[3],
        "status": row[4],
        "latency_ms": row[5],
        "prompt_tokens": row[6],
        "completion_tokens": row[7],
        "total_tokens": row[8],
        "request": row[9],
        "response": row[10],
        "error": row[11],
        "trace": row[12],
        "interview_id": row[13],
        "category": row[14],
    }


def stats() -> dict[str, Any]:
    with _lock:
        db = _db()
        row = db.execute(
            """SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(AVG(latency_ms),0),
                      SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END)
               FROM llm_calls"""
        ).fetchone()
        today = db.execute(
            """SELECT COUNT(*), COALESCE(SUM(total_tokens),0)
               FROM llm_calls WHERE created_at >= ?""",
            (_today_start_utc(),),
        ).fetchone()
    return {
        "total_calls": row[0],
        "total_tokens": row[1],
        "avg_latency_ms": round(row[2] or 0),
        "errors": row[3] or 0,
        "today_calls": today[0],
        "today_tokens": today[1],
    }


def clear() -> int:
    with _lock:
        db = _db()
        count = db.execute("DELETE FROM llm_calls").rowcount
        db.commit()
    return count
