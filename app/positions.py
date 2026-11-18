"""岗位管理：岗位名称、JD、技能要求。面试启动时作为上下文传给意图识别和话术。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("positions")

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
            """CREATE TABLE IF NOT EXISTS positions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL,
                   jd TEXT NOT NULL DEFAULT '',
                   skills TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        _conn.commit()
        _seed(_conn)
    return _conn


def _seed(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]:
        return
    now = _now()
    db.execute(
        "INSERT INTO positions (name, jd, skills, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            "RAG 算法工程师",
            "负责企业知识库问答系统的检索与生成链路，要求有 RAG 落地经验，能独立完成效果评估与优化。",
            "RAG、向量检索、Embedding、Rerank、Prompt 工程、Python",
            now,
            now,
        ),
    )
    db.commit()
    log.info("岗位表已写入示例岗位")


def list_items() -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT id, name, jd, skills, updated_at FROM positions ORDER BY id").fetchall()
    return [{"id": r[0], "name": r[1], "jd": r[2], "skills": r[3], "updated_at": r[4]} for r in rows]


def add(name: str, jd: str, skills: str) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("岗位名称不能为空")
    now = _now()
    with _lock:
        db = _db()
        cursor = db.execute(
            "INSERT INTO positions (name, jd, skills, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, jd, skills, now, now),
        )
        db.commit()
        return {"id": int(cursor.lastrowid or 0), "name": name, "jd": jd, "skills": skills, "updated_at": now}


def update(item_id: int, name: str, jd: str, skills: str) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute(
            "UPDATE positions SET name = ?, jd = ?, skills = ?, updated_at = ? WHERE id = ?",
            (name.strip(), jd, skills, _now(), item_id),
        )
        db.commit()
    return cursor.rowcount > 0


def delete(item_id: int) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute("DELETE FROM positions WHERE id = ?", (item_id,))
        db.commit()
    return cursor.rowcount > 0


def context_text(item_id: int) -> tuple[str, str]:
    """返回 (岗位名, 拼好的背景文本)，给会话配置用。"""
    with _lock:
        row = _db().execute("SELECT name, jd, skills FROM positions WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return "", ""
    name, jd, skills = row
    parts = []
    if jd.strip():
        parts.append(f"岗位职责：{jd.strip()}")
    if skills.strip():
        parts.append(f"技能要求：{skills.strip()}")
    return name, "\n".join(parts)
