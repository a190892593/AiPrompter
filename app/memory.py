"""意图识别经验库：把人工纠错存下来，下次意图识别时当 few-shot 参考。

这不是重新训练模型，而是"记忆 + 相似检索"的持续改进：
每纠正一次，同类场景下次就会被召回进 prompt，意图识别随之变准。
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("interview.memory")

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
            """CREATE TABLE IF NOT EXISTS judge_feedback (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   session_id TEXT,
                   last_text TEXT NOT NULL,
                   context TEXT,
                   predicted_type TEXT,
                   correct_type TEXT NOT NULL,
                   corrected_question TEXT,
                   note TEXT
               )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_text ON judge_feedback(last_text)")
        _conn.commit()
    return _conn


def add(record: dict[str, Any]) -> int:
    with _lock:
        db = _db()
        cursor = db.execute(
            """INSERT INTO judge_feedback
               (created_at, session_id, last_text, context, predicted_type, correct_type, corrected_question, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(),
                record.get("session_id", ""),
                record.get("last_text", ""),
                record.get("context", ""),
                record.get("predicted_type", ""),
                record.get("correct_type", "statement"),
                record.get("corrected_question", ""),
                record.get("note", ""),
            ),
        )
        db.commit()
        log.info(
            "意图识别经验已入库: id=%s 纠正为=%s 文本=%r",
            cursor.lastrowid,
            record.get("correct_type"),
            str(record.get("last_text", ""))[:60],
        )
        return int(cursor.lastrowid or 0)


def similar(last_text: str, limit: int = 3) -> list[dict[str, Any]]:
    """按文本相似度召回历史纠错，优先返回"识别错了"的样本。"""
    if not last_text.strip():
        return []
    with _lock:
        rows = _db().execute(
            """SELECT last_text, correct_type, corrected_question, predicted_type
               FROM judge_feedback ORDER BY id DESC LIMIT 400"""
        ).fetchall()

    scored: list[tuple[float, dict[str, Any]]] = []
    for stored_text, correct_type, corrected_question, predicted_type in rows:
        ratio = difflib.SequenceMatcher(None, last_text, stored_text).ratio()
        if ratio < 0.32:
            continue
        bonus = 0.15 if predicted_type and predicted_type != correct_type else 0.0
        scored.append(
            (
                ratio + bonus,
                {
                    "last_text": stored_text,
                    "correct_type": correct_type,
                    "corrected_question": corrected_question or "",
                    "predicted_type": predicted_type,
                },
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for _, item in scored:
        if item["last_text"] in seen:
            continue
        seen.add(item["last_text"])
        result.append(item)
        if len(result) >= limit:
            break
    log.debug("意图识别经验召回: 文本=%r 命中=%d 条（候选 %d）", last_text[:40], len(result), len(rows))
    return result


def stats() -> dict[str, Any]:
    with _lock:
        db = _db()
        total = db.execute("SELECT COUNT(*) FROM judge_feedback").fetchone()[0]
        corrected = db.execute(
            "SELECT COUNT(*) FROM judge_feedback WHERE predicted_type != correct_type AND predicted_type != ''"
        ).fetchone()[0]
        rows = db.execute(
            "SELECT correct_type, COUNT(*) FROM judge_feedback GROUP BY correct_type"
        ).fetchall()
    return {
        "total_feedback": total,
        "type_corrections": corrected,
        "by_type": {name: count for name, count in rows},
    }
