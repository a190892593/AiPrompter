"""统一日志：控制台 + 滚动文件。

为什么单独一层：之前意图识别抛异常时只有一行 exception 落在控制台，窗口一关就没了，
复现问题时只能靠猜。现在关键操作都写进同一个文件（默认 `data/logs/app.log`），
出问题直接把日志发出来即可。

开关都在 `.env`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARNING / ERROR |
| `LOG_FILE` | 1 | 0 只输出到控制台 |
| `LOG_DIR` | data/logs | 日志目录 |
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import settings

_configured = False

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-22s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def mask(value: str | None) -> str:
    """把 Key 打成可以出日志的样子：只留前 4 位和后 4 位。"""
    text = (value or "").strip()
    if not text:
        return "未配置"
    if len(text) <= 10:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}({len(text)}位)"


def setup(level: str | None = None) -> None:
    """配好根 logger；重复调用无副作用，任何入口都能安全调一次。"""
    global _configured
    if _configured:
        return
    _configured = True

    name = (level or settings.log_level or "INFO").strip().upper()
    level_value = getattr(logging, name, logging.INFO)

    formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)
    root = logging.getLogger()
    for handler in list(root.handlers):  # 去掉 basicConfig / 重复初始化留下的 handler
        root.removeHandler(handler)
    # 根 logger 开 DEBUG：控制台按 LOG_LEVEL 过滤，文件始终留全量（DEBUG），
    # 这样排查时不用重启改级别，直接看文件里最细的那层。
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level_value)
    root.addHandler(console)

    if settings.log_file:
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                settings.log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.DEBUG)
            root.addHandler(file_handler)
        except Exception as exc:  # noqa: BLE001 - 日志文件写不了也得能跑
            root.warning("日志文件不可用（只输出到控制台）：%s", exc)

    # uvicorn 默认给自己的 logger 挂 handler：不接管会重复打印，而且不进日志文件
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level_value)

    # 第三方库太吵（httpx 每个 TCP 包一行），统一压到 WARNING
    for logger_name in ("httpx", "httpcore", "websockets", "asyncio", "multipart", "urllib3"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # jieba 被 import 时会把自身级别强设成 DEBUG 并挂一个 stderr handler，每次分词都刷屏。
    # 用拒绝式 filter 而不是只改级别：getLogger("jieba").setLevel 会被 jieba 自己覆盖。
    logging.getLogger("jieba").addFilter(_Silent())


class _Silent(logging.Filter):
    """丢弃该 logger 的全部记录（含 jieba 自带的 stderr handler）。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return False
