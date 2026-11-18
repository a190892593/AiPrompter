"""简历库：保存多份个人简历，新建面试时下拉选择。

面试开始后简历内容会作为「我的背景」传给意图识别和话术。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("resumes")

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
            """CREATE TABLE IF NOT EXISTS resumes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL,
                   content TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        _conn.commit()
        _seed(_conn)
    return _conn


def _seed(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]:
        return
    now = _now()
    db.execute(
        "INSERT INTO resumes (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (
            "示例简历",
            "3 年 RAG/搜索方向经验：\n"
            "- 负责企业知识库问答：文档切分、向量索引、召回排序全链路\n"
            "- 召回率从 62% 提到 81%，线上采纳率提升 12 个点\n"
            "- 技术栈：Python、Faiss、Milvus、bge 系列 embedding、rerank",
            now,
            now,
        ),
    )
    db.commit()
    log.info("简历库已写入示例简历")


def list_items() -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT id, name, updated_at FROM resumes ORDER BY id").fetchall()
    return [{"id": r[0], "name": r[1], "updated_at": r[2]} for r in rows]


def get(item_id: int) -> dict[str, Any] | None:
    with _lock:
        row = _db().execute(
            "SELECT id, name, content, updated_at FROM resumes WHERE id = ?", (item_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "content": row[2], "updated_at": row[3]}


def add(name: str, content: str) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("简历名称不能为空")
    now = _now()
    with _lock:
        db = _db()
        cursor = db.execute(
            "INSERT INTO resumes (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, content, now, now),
        )
        db.commit()
    return {"id": int(cursor.lastrowid or 0), "name": name, "content": content, "updated_at": now}


def update(item_id: int, name: str, content: str) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute(
            "UPDATE resumes SET name = ?, content = ?, updated_at = ? WHERE id = ?",
            (name.strip(), content, _now(), item_id),
        )
        db.commit()
    return cursor.rowcount > 0


def delete(item_id: int) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute("DELETE FROM resumes WHERE id = ?", (item_id,))
        db.commit()
    return cursor.rowcount > 0
