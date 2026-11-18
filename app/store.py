"""会话持久化：把转写、问题卡片、报告落 SQLite，刷新/重启后能恢复。

只存 JSON 文档，不建复杂表：一次面试一个 doc，够用且好改。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("store")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   id TEXT PRIMARY KEY,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   doc TEXT NOT NULL
               )"""
        )
        _conn.commit()
    return _conn


def save(doc: dict[str, Any]) -> None:
    payload = json.dumps(doc, ensure_ascii=False)
    with _lock:
        db = _db()
        db.execute(
            """INSERT INTO sessions (id, created_at, updated_at, doc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, doc=excluded.doc""",
            (doc["id"], doc.get("created_at") or _now(), _now(), payload),
        )
        db.commit()
    log.debug("会话落库: id=%s %d 字节", doc.get("id"), len(payload))


def latest(limit: int = 1) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT doc FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    docs = [json.loads(row[0]) for row in rows]
    log.debug("读取最近会话: 需求=%d 实际=%d", limit, len(docs))
    return docs


def get(session_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _db().execute("SELECT doc FROM sessions WHERE id = ?", (session_id,)).fetchone()
    log.debug("读取会话 %s: %s", session_id, "命中" if row else "不存在")
    return json.loads(row[0]) if row else None


def delete(session_id: str) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        db.commit()
    log.info("删除历史会话 %s: %s", session_id, "成功" if cursor.rowcount > 0 else "不存在")
    return cursor.rowcount > 0


def summaries(limit: int = 50) -> list[dict[str, Any]]:
    """历史会话列表（不含大字段）。"""
    with _lock:
        rows = _db().execute(
            "SELECT id, created_at, updated_at, doc FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    items = []
    for session_id, created_at, updated_at, raw in rows:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        questions = doc.get("questions") or []
        report = doc.get("report") or {}
        stamps = [float(u.get("ts") or 0) for u in (doc.get("utterances") or [])]
        stamps += [float(q.get("ts") or 0) for q in questions]
        stamps = [t for t in stamps if t > 0]
        duration = int(max(stamps) - min(stamps)) if len(stamps) >= 2 else 0
        items.append(
            {
                "id": session_id,
                "created_at": created_at,
                "updated_at": updated_at,
                "role": (doc.get("config") or {}).get("role", ""),
                "engine": doc.get("engine", ""),
                "turns": len(questions),
                "utterances": len(doc.get("utterances") or []),
                "score": report.get("overall_score"),
                "duration": duration,
                "verdict": report.get("verdict", ""),
            }
        )
    return items


def cleanup(mode: str = "short") -> int:
    """清理会话。

    all   ：全部删除
    empty ：删掉没有任何内容的场次
    short ：删掉"题少于 2 且句少于 2"的短场次（多为测试/误触）
    """
    items = summaries(limit=10000)
    if mode == "all":
        targets = [item["id"] for item in items]
    elif mode == "empty":
        targets = [item["id"] for item in items if item["turns"] == 0 and item["utterances"] == 0]
    else:
        targets = [
            item["id"]
            for item in items
            if item["turns"] < 2 and item["utterances"] < 2
        ]

    deleted = 0
    with _lock:
        db = _db()
        for session_id in targets:
            cursor = db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            deleted += cursor.rowcount
        db.commit()
    return deleted
