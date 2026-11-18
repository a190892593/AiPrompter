"""本地语义向量：bge-small-zh-v1.5（ONNX 量化版，约 23MB），纯 CPU 离线跑。

用途：题库召回。BM25 解决"关键词对得上"，这一层解决"说法不一样但意思一样"。
embeddings 会缓存进 SQLite，题库不变就不会重复算。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import numpy as np

from .config import PROJECT_DIR, settings

log = logging.getLogger("embedding")

MODEL_DIR = PROJECT_DIR / "models" / "bge-small-zh-v1.5"
_lock = threading.RLock()
_session = None
_tokenizer = None
_dim = 0
_conn: sqlite3.Connection | None = None
_failed = False


def available() -> bool:
    return (MODEL_DIR / "model.onnx").is_file() and (MODEL_DIR / "tokenizer.json").is_file()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, dim INTEGER NOT NULL, vec BLOB NOT NULL)"
        )
        _conn.commit()
    return _conn


def _load() -> bool:
    """懒加载模型；失败只记一次日志，之后直接返回 False 走纯词法召回。"""
    global _session, _tokenizer, _dim, _failed
    if _session is not None:
        return True
    if _failed:
        return False

    with _lock:
        if _session is not None:
            return True
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.log_severity_level = 3
            _session = ort.InferenceSession(
                str(MODEL_DIR / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
            )
            _tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
            _tokenizer.enable_truncation(max_length=512)
            _tokenizer.enable_padding()
            _dim = int(_session.get_outputs()[0].shape[-1])
            log.info("语义向量模型已加载: %s 维", _dim)
            return True
        except Exception as exc:  # noqa: BLE001 - 模型坏了也不能拖垮召回
            _failed = True
            log.warning("语义向量模型不可用，退回纯词法召回: %s", exc)
            return False


def encode(texts: list[str]) -> np.ndarray:
    """把一批文本编码成 L2 归一化后的向量（bge 用 CLS pooling）。"""
    if not texts or not _load():
        return np.zeros((0, 0), dtype=np.float32)

    encodings = _tokenizer.encode_batch([t or "" for t in texts])
    ids = np.array([e.ids for e in encodings], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    types = np.zeros_like(ids)

    feeds = {"input_ids": ids, "attention_mask": mask}
    names = {item.name for item in _session.get_inputs()}
    if "token_type_ids" in names:
        feeds["token_type_ids"] = types

    hidden = _session.run(None, feeds)[0]
    vectors = hidden[:, 0, :]  # CLS
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def embed_cached(texts: list[str]) -> dict[str, np.ndarray]:
    """带 SQLite 缓存的批量编码，返回 {原文: 向量}。"""
    result: dict[str, np.ndarray] = {}
    if not texts or not _load():
        return result

    missing: list[str] = []
    db = _db()
    for text in texts:
        key = _key(text)
        row = db.execute("SELECT dim, vec FROM embeddings WHERE key = ?", (key,)).fetchone()
        if row and int(row[0]) == _dim:
            result[text] = np.frombuffer(row[1], dtype=np.float32).copy()
        else:
            missing.append(text)

    if missing:
        vectors = encode(missing)
        with _lock:
            for text, vec in zip(missing, vectors):
                result[text] = vec
                db.execute(
                    "INSERT OR REPLACE INTO embeddings (key, dim, vec) VALUES (?, ?, ?)",
                    (_key(text), _dim, vec.astype(np.float32).tobytes()),
                )
            db.commit()

    return result


def similarity(query_vector: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    """余弦相似度（两边都已归一化，直接点积）。"""
    if query_vector.size == 0 or vectors.size == 0:
        return np.zeros(vectors.shape[0], dtype=np.float32)
    return np.asarray(vectors @ query_vector, dtype=np.float32)
