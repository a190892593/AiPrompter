"""运行时设置：界面上能改的配置项，存 SQLite，启动时套用到 settings 单例。

只暴露"改了立刻有用"的项；像端口、模型路径这种要重启的仍走 .env。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("settings")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

# key -> (类型, 分组, 说明, 是否敏感)
SCHEMA: list[tuple[str, str, str, str, bool]] = [
    ("llm_base_url", "str", "大模型", "OpenAI 兼容接口地址", False),
    ("llm_api_key", "secret", "大模型", "Key（留空表示不修改）", True),
    ("llm_model", "str", "大模型", "意图识别模型", False),
    ("llm_answer_model", "str", "大模型", "参考答案模型（留空跟随意图识别）", False),
    ("llm_log_enabled", "bool", "大模型", "是否记录每次调用的出入参", False),
    ("llm_thinking", "bool", "大模型", "深度档是否启用思考（自动答案只有深度档走 reasoning，极简/标准不思考）", False),
    ("asr_provider", "str", "语音识别", "auto / local / cloud / volc / deepseek / mock", False),
    ("volc_speech_key", "secret", "语音识别", "火山豆包语音 Key", True),
    ("volc_resource_id", "str", "语音识别", "资源 ID（seedasr=2.0，bigasr=1.0）", False),
    ("vad_silence_ms", "int", "采集", "静音多久算说完（毫秒）", False),
    ("vad_threshold", "float", "采集", "能量门限（越大越不灵敏）", False),
    ("judge_confidence", "float", "意图识别", "置信度门槛（低于不出参考答案）", False),
    ("intent_verify", "bool", "意图识别", "低置信度时异步复核（先出卡，复核不过再撤卡）", False),
    ("intent_verify_below", "float", "意图识别", "低于该置信度触发二次校验", False),
    ("report_thinking", "bool", "评分", "评分报告是否启用思考（慢一些，点评更细）", False),
    ("answer_level", "str", "参考答案", "默认档位 brief / standard / deep", False),
    ("tts_engine", "str", "仿真语音", "auto / sapi（本机）/ edge（在线）", False),
    ("sim_question_limit", "int", "仿真", "一场仿真最多问多少题（默认 20，避免浪费 token）", False),
]

SECRET_KEYS = {key for key, _, _, _, secret in SCHEMA if secret}

# 数值型设置的合法范围（超出直接拒绝，避免存进库后每次启动都报错）
RANGES: dict[str, tuple[float, float]] = {
    "vad_silence_ms": (200, 5000),
    "vad_threshold": (0.0, 0.5),
    "judge_confidence": (0.0, 1.0),
    "intent_verify_below": (0.3, 1.0),
    "sim_question_limit": (1, 200),
}

ENUMS: dict[str, set[str]] = {
    "asr_provider": {"auto", "local", "cloud", "volc", "deepseek", "mock"},
    "answer_level": {"brief", "standard", "deep"},
    "tts_engine": {"auto", "sapi", "edge"},
}


def _validate(key: str, kind: str, raw: str) -> str:
    """校验并规范化，非法值直接抛 ValueError（接口返回 400）。"""
    try:
        value = _cast(kind, raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 需要{kind}格式，收到「{raw}」") from exc

    if key in RANGES:
        low, high = RANGES[key]
        if not (low <= float(value) <= high):
            raise ValueError(f"{key} 需要在 {low} ~ {high} 之间，收到「{raw}」")

    if key in ENUMS and str(value).strip() not in ENUMS[key]:
        raise ValueError(f"{key} 只能是 {' / '.join(sorted(ENUMS[key]))}，收到「{raw}」")

    return raw


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _conn.commit()
    return _conn


def _cast(kind: str, raw: str) -> Any:
    if kind in ("int",):
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    return str(raw)


# 记下 .env 里的原始值：清空覆盖时要能回落到这里
_ORIGINALS: dict[str, Any] = {}


def _capture_originals() -> None:
    for key, *_ in SCHEMA:
        _ORIGINALS.setdefault(key, getattr(settings, key, ""))


_capture_originals()


def _raw_overrides() -> dict[str, str]:
    with _lock:
        rows = _db().execute("SELECT key, value FROM settings").fetchall()
    return dict(rows)


def apply_all() -> None:
    """把库里保存过的覆盖值套到 settings 单例上（启动时调一次）。"""
    overrides = _raw_overrides()
    applied: list[str] = []
    for key, kind, *_ in SCHEMA:
        if key in overrides:
            try:
                setattr(settings, key, _cast(kind, overrides[key]))
                applied.append(key)
            except Exception:  # noqa: BLE001
                log.warning("设置项 %s 应用失败（存的值=%r）", key, overrides[key])
        else:
            # 没有覆盖就回落到 .env 的原始值，避免"清空后仍是旧值"
            setattr(settings, key, _ORIGINALS.get(key, getattr(settings, key, "")))
    if applied:
        log.info("已套用界面保存的设置: %s", ", ".join(applied))


def public() -> list[dict[str, Any]]:
    """给界面用：敏感项返回是否已填，不返回明文。"""
    overrides = _raw_overrides()
    items = []
    for key, kind, group, desc, secret in SCHEMA:
        value = getattr(settings, key, "")
        if secret:
            shown = "已配置" if str(value).strip() else "未配置"
        elif kind == "bool":
            shown = "开" if value else "关"
        else:
            shown = str(value)
        items.append(
            {
                "key": key,
                "kind": kind,
                "group": group,
                "desc": desc,
                "secret": secret,
                "value": shown,
                "overridden": key in overrides,
            }
        )
    return items


def save(values: dict[str, str]) -> dict[str, Any]:
    """保存并立即生效。空字符串 = 清空覆盖、回落到 .env。"""
    changed: list[str] = []
    with _lock:
        db = _db()
        for key, kind, *_ in SCHEMA:
            if key not in values:
                continue
            raw = str(values[key]).strip()
            if raw == "":
                db.execute("DELETE FROM settings WHERE key = ?", (key,))
                continue
            raw = _validate(key, kind, raw)
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, raw),
            )
            changed.append(key)
        db.commit()

    apply_all()

    # 接口地址变了要重建 HTTP 客户端，否则还连着旧域名
    if "llm_base_url" in changed or "llm_api_key" in changed:
        try:
            from .llm import llm

            llm.reset_client()
        except Exception:  # noqa: BLE001
            log.warning("重建 LLM 客户端失败", exc_info=True)

    return {"ok": True, "changed": changed}
