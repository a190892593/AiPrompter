"""面试题库：常见面试题 + 参考答案，可增删改。

首次启动时会用 tools/questions-rag-agent.txt 做一次种子导入。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time

import numpy as np
import threading
from datetime import datetime, timezone
from typing import Any

from .config import PROJECT_DIR, settings

log = logging.getLogger("bank")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_SEED_FILE = PROJECT_DIR / "tools" / "questions-rag-agent.txt"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS bank (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   category TEXT NOT NULL DEFAULT '通用',
                   question TEXT NOT NULL,
                   answer TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        _conn.commit()
        _seed_if_empty(_conn)
    return _conn


def _seed_if_empty(db: sqlite3.Connection) -> None:
    count = db.execute("SELECT COUNT(*) FROM bank").fetchone()[0]
    if count or not _SEED_FILE.is_file():
        return
    try:
        lines = [line.strip() for line in _SEED_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return
    items = [line for line in lines if line and not line.startswith("#")]
    if not items:
        return
    now = _now()
    db.executemany(
        "INSERT INTO bank (category, question, answer, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
        [("RAG / Agent", q, now, now) for q in items],
    )
    db.commit()
    log.info("题库已导入 %s 条种子题目", len(items))


def list_items(category: str = "") -> list[dict[str, Any]]:
    with _lock:
        db = _db()
        if category:
            rows = db.execute(
                "SELECT id, category, question, answer, updated_at FROM bank WHERE category = ? ORDER BY id",
                (category,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, category, question, answer, updated_at FROM bank ORDER BY id"
            ).fetchall()
    return [
        {"id": r[0], "category": r[1], "question": r[2], "answer": r[3], "updated_at": r[4]}
        for r in rows
    ]


def categories() -> list[str]:
    with _lock:
        rows = _db().execute("SELECT DISTINCT category FROM bank ORDER BY category").fetchall()
    return [r[0] for r in rows]


def add(category: str, question: str, answer: str = "") -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise ValueError("题目不能为空")
    now = _now()
    with _lock:
        db = _db()
        cursor = db.execute(
            "INSERT INTO bank (category, question, answer, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (category.strip() or "通用", question, answer, now, now),
        )
        db.commit()
        return {
            "id": int(cursor.lastrowid or 0),
            "category": category,
            "question": question,
            "answer": answer,
            "updated_at": now,
        }


def update(item_id: int, category: str, question: str, answer: str) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute(
            "UPDATE bank SET category = ?, question = ?, answer = ?, updated_at = ? WHERE id = ?",
            (category.strip() or "通用", question.strip(), answer, _now(), item_id),
        )
        db.commit()
    return cursor.rowcount > 0


def delete(item_id: int) -> bool:
    with _lock:
        db = _db()
        cursor = db.execute("DELETE FROM bank WHERE id = ?", (item_id,))
        db.commit()
    return cursor.rowcount > 0


def pick_questions(limit: int = 10, category: str = "") -> list[str]:
    """随机取题目文本，供仿真面试使用。

    分类只是"优先"，不是硬过滤：岗位分类下题不够时从题库其它分类补足。
    limit <= 0 表示不设上限，取题库里全部去重后的题目（仿真一直问到用户停止）。
    """
    import random

    # 先按题目文本去重，避免同一道题在不同分类里被抽到两次
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list_items():
        text = (item.get("question") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        pool.append({**item, "question": text})

    if not pool:
        return []

    limit = len(pool) if limit <= 0 else max(1, limit)

    preferred = [item for item in pool if category and item["category"] == category]
    rest = [item for item in pool if item not in preferred]

    if len(preferred) >= limit:
        picked = random.sample(preferred, limit)
    else:
        picked = list(preferred) + random.sample(rest, min(limit - len(preferred), len(rest)))

    random.shuffle(picked)
    if category and len(picked) > len(preferred):
        log.info(
            "仿真选题: 分类=%r 命中 %d 道，从其它分类补足 %d 道，共 %d 道",
            category,
            len(preferred),
            len(picked) - len(preferred),
            len(picked),
        )
    return [item["question"] for item in picked]


def _tokens(text: str) -> list[str]:
    """中文分词 + 去停用词，供 BM25 使用。没有 jieba 时退回字符二元组。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        import jieba

        words = [w.strip().lower() for w in jieba.lcut(text)]
    except Exception:  # noqa: BLE001 - 没装 jieba 也要能跑
        return [text[i : i + 2] for i in range(max(1, len(text) - 1))]

    stop = {
        "的", "了", "吗", "呢", "吧", "啊", "是", "在", "和", "与", "或", "有", "没", "不",
        "你", "我", "他", "你们", "我们", "他们", "这", "那", "个", "怎么", "什么", "如何",
        "为", "什么", "请问", "一下", "进行", "可以", "需要", "应该", "以及", "还有",
        "？", "?", "。", "，", ",", "、", "：", ":", "；", ";", "！", "!", "（", "）", "(", ")",
    }
    return [w for w in words if w and w not in stop and not w.isspace()]


def _bm25(query_tokens: list[str], docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """经典 BM25：词频饱和 + 文档长度归一 + IDF 加权。"""
    from collections import Counter

    if not docs_tokens or not query_tokens:
        return [0.0] * len(docs_tokens)

    total = len(docs_tokens)
    avg_len = sum(len(doc) for doc in docs_tokens) / total or 1.0
    doc_freq: Counter = Counter()
    for doc in docs_tokens:
        for token in set(doc):
            doc_freq[token] += 1

    scores: list[float] = []
    for doc in docs_tokens:
        freq = Counter(doc)
        score = 0.0
        for token in query_tokens:
            if token not in freq:
                continue
            idf = math.log(1 + (total - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
            tf = freq[token]
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
        scores.append(score)
    return scores


def search_lexical(text: str, limit: int = 2, threshold: float = 0.34) -> list[dict[str, Any]]:
    """旧实现（仅字符相似度），保留下来做对比基线。"""
    import difflib

    text = (text or "").strip()
    if not text:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in list_items():
        if not item["answer"].strip():
            continue
        ratio = difflib.SequenceMatcher(None, text, item["question"]).ratio()
        if ratio >= threshold:
            scored.append((ratio, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def search(text: str, limit: int = 2, threshold: float = 0.35) -> list[dict[str, Any]]:
    """混合召回：语义向量 × BM25 × 字符相似度。

    - 语义向量（bge-small-zh）负责"说法不同但意思相同"；
    - BM25 负责"关键词对得上"，避免被语义模型带偏；
    - 字符相似度兜底，防止短查询被向量相似度抹平。
    三者归一后加权，都为 0 的直接丢弃。
    """
    import difflib

    started = time.perf_counter()
    text = (text or "").strip()
    if not text:
        return []

    items = [item for item in list_items() if item["answer"].strip()]
    if not items:
        log.info("题库召回: 题库里没有「带答案」的题，本次无参考")
        return []

    # 词法
    query_tokens = _tokens(text)
    docs_tokens = [_tokens(item["question"]) for item in items]
    bm25 = _bm25(query_tokens, docs_tokens)
    peak = max(bm25) if bm25 else 0.0

    # 语义
    semantic = None
    try:
        from . import embedding

        if embedding.available():
            cache = embedding.embed_cached([item["question"] for item in items])
            query_vec = embedding.encode([text])
            if cache and query_vec.size:
                matrix = np.stack([cache[item["question"]] for item in items])
                semantic = embedding.similarity(query_vec[0], matrix)
    except Exception:  # noqa: BLE001 - 语义层挂了也要能召回
        semantic = None

    # 语义 0.6 + 词法 0.4：实测权重从 0.5 调到 1.0 结果几乎不变，
    # 说明瓶颈在语义模型本身，不在融合比例，取简单可解释的固定权重。
    scored: list[tuple[float, dict[str, Any]]] = []
    for index, item in enumerate(items):
        keyword = (bm25[index] / peak) if peak > 0 else 0.0
        wording = difflib.SequenceMatcher(None, text, item["question"]).ratio()
        lexical = 0.65 * keyword + 0.35 * wording
        dense = max(0.0, min(1.0, float(semantic[index]))) if semantic is not None else 0.0

        if semantic is not None:
            # 语义够近就保留；三条都不沾边才丢，避免误杀靠语义才捞回的题
            if dense < 0.38 and lexical < 0.5:
                continue
            fused = 0.6 * dense + 0.4 * lexical
        else:
            if lexical < 0.5:
                continue
            fused = lexical

        if fused >= 0.20:
            scored.append((fused, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [item for _, item in scored[:limit]]
    log.info(
        "题库召回: 查询=%r 候选=%d条 语义=%s 命中=%d条 用时=%.0fms",
        text[:60],
        len(items),
        "开" if semantic is not None else "关（向量模型不可用，退回词法）",
        len(top),
        (time.perf_counter() - started) * 1000,
    )
    if top:
        log.debug(
            "题库召回得分: %s",
            [(round(score, 2), item["question"][:30]) for score, item in scored[:limit]],
        )
    return top


def search_multi(queries: list[str], limit: int = 2) -> list[dict[str, Any]]:
    """多路检索（问题 + 意图识别给出的检索改写），按名次交错合并去重。"""
    lanes: list[list[dict[str, Any]]] = []
    for query in queries:
        text_value = (query or "").strip()
        if not text_value:
            continue
        hits = search(text_value, limit)
        if hits:
            lanes.append(hits)
    if not lanes:
        return []

    # 多路共识：同时被多条检索式命中的题优先，其次看最早名次
    scores: dict[int, float] = {}
    first_rank: dict[int, int] = {}
    items_by_id: dict[int, dict[str, Any]] = {}
    for lane in lanes:
        for rank, item in enumerate(lane):
            item_id = item["id"]
            items_by_id.setdefault(item_id, item)
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank + 1)
            first_rank[item_id] = min(first_rank.get(item_id, rank), rank)
    ranked = sorted(
        scores,
        key=lambda item_id: (-scores[item_id], first_rank[item_id]),
    )
    merged = [items_by_id[item_id] for item_id in ranked[:limit]]
    if merged:
        log.info(
            "多路召回: 查询=%d条 合并=%s",
            len(lanes),
            [item["question"][:24] for item in merged],
        )
    return merged


def warmup() -> None:
    """预热分词与向量模型，避免第一次检索把加载时间算进首字延迟。"""
    started = time.perf_counter()
    try:
        _tokens("预热")
    except Exception:  # noqa: BLE001
        log.debug("预热分词失败", exc_info=True)
    try:
        from . import embedding

        if embedding.available():
            embedding.encode(["预热"])
    except Exception:  # noqa: BLE001
        log.debug("预热向量模型失败", exc_info=True)
    log.info("检索预热完成: 用时=%.0fms", (time.perf_counter() - started) * 1000)
