"""跨场记忆：把每场面试评出来的短板/亮点累积下来，下次开局就带上。

这是"越用越聪明"里最实在的一层——不靠模型自我进化，靠真的记住你上次栽在哪。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("profile")

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
            """CREATE TABLE IF NOT EXISTS profile (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind TEXT NOT NULL,
                   content TEXT NOT NULL,
                   hits INTEGER NOT NULL DEFAULT 1,
                   first_seen TEXT NOT NULL,
                   last_seen TEXT NOT NULL
               )"""
        )
        _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_profile ON profile(kind, content)")
        _conn.commit()
    return _conn


def remember(kind: str, items: list[str]) -> int:
    """把一批条目记进长期画像，重复出现的累加次数。"""
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return 0
    now = _now()
    added = 0
    with _lock:
        db = _db()
        for content in cleaned:
            db.execute(
                """INSERT INTO profile (kind, content, hits, first_seen, last_seen)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(kind, content) DO UPDATE SET hits = hits + 1, last_seen = excluded.last_seen""",
                (kind, content, now, now),
            )
            added += 1
        db.commit()
    log.debug("长期画像已记录 %s: %d 条", kind, added)
    return added


def top(kind: str, limit: int = 3) -> list[str]:
    """取最常出现的条目（次数优先，其次最近）。"""
    with _lock:
        rows = _db().execute(
            "SELECT content FROM profile WHERE kind = ? ORDER BY hits DESC, last_seen DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [row[0] for row in rows]


def snapshot() -> dict[str, list[str]]:
    with _lock:
        rows = _db().execute(
            "SELECT kind, content FROM profile ORDER BY kind, hits DESC"
        ).fetchall()
    grouped: dict[str, list[str]] = {}
    for kind, content in rows:
        grouped.setdefault(kind, []).append(content)
    return grouped


def memory_text(limit: int = 3) -> str:
    """拼成一段给模型看的历史记忆。"""
    gaps = top("gap", limit)
    strengths = top("strength", limit)
    lines = []
    if gaps:
        lines.append("过往面试反复出现的短板：" + "；".join(gaps))
    if strengths:
        lines.append("过往表现较好的地方：" + "；".join(strengths))
    text = "\n".join(lines)
    log.debug("长期记忆注入: 短板=%d 亮点=%d", len(gaps), len(strengths))
    return text
