"""火山引擎「豆包流式语音识别 2.0」适配器。

协议要点（v3 SAUC 双向流式，二进制 WebSocket）：
- 握手头：X-Api-Key / X-Api-Resource-Id / X-Api-Request-Id / X-Api-Sequence
- 资源 ID：小时版 volc.seedasr.sauc.duration，并发版 volc.seedasr.sauc.concurrent
- 端点：wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
- 帧格式：4 字节头 + [4 字节序号] + 4 字节 payload 长度 + payload
  byte0 = 版本<<4 | 头长度(单位 4 字节)
  byte1 = 消息类型<<4 | flags
  byte2 = 序列化<<4 | 压缩

本类对上层暴露同步的 push/reset/close：
- push 只把 PCM 塞进队列，立刻返回当前累计文本，绝不阻塞采集线程；
- 收发在独立线程里跑，避免和 asyncio 事件循环抢资源。
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
import uuid
from typing import Any

from websockets.sync.client import connect

log = logging.getLogger("asr.volc")

PROTOCOL_VERSION = 0x01
DEFAULT_HEADER_SIZE = 0x01

MSG_FULL_CLIENT = 0x01
MSG_AUDIO_ONLY = 0x02
MSG_FULL_SERVER = 0x09
MSG_SERVER_ACK = 0x0B
MSG_ERROR = 0x0F

FLAG_NONE = 0x00
FLAG_LAST = 0x02

SERIAL_JSON = 0x01
SERIAL_NONE = 0x00
COMPRESS_GZIP = 0x01
COMPRESS_NONE = 0x00


def build_frame(
    message_type: int,
    payload: bytes,
    *,
    serialization: int = SERIAL_JSON,
    compression: int = COMPRESS_GZIP,
    flags: int = FLAG_NONE,
) -> bytes:
    header = bytes(
        [
            (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        ]
    )
    body = gzip.compress(payload) if compression == COMPRESS_GZIP else payload
    return header + len(body).to_bytes(4, "big") + body


def parse_frame(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        return {"type": 0, "payload": b""}
    header_words = data[0] & 0x0F
    offset = header_words * 4
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F

    if flags & 0x01 and len(data) >= offset + 4:
        offset += 4  # 带序号
    if len(data) < offset + 4:
        return {"type": message_type, "payload": b""}

    size = int.from_bytes(data[offset : offset + 4], "big")
    offset += 4
    body = data[offset : offset + size]

    if compression == COMPRESS_GZIP:
        try:
            body = gzip.decompress(body)
        except OSError:
            pass

    parsed: Any = body
    if serialization == SERIAL_JSON:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
    return {"type": message_type, "payload": parsed}


class VolcSpeechEngine:
    name = "volc:seedasr-2.0"

    def __init__(
        self,
        api_key: str,
        app_id: str,
        resource_id: str,
        url: str,
        sample_rate: int = 16000,
    ) -> None:
        if not api_key:
            raise RuntimeError("未配置 VOLC_SPEECH_KEY")
        self._api_key = api_key
        self._app_id = app_id
        self._resource_id = resource_id
        self._url = url
        self._sample_rate = sample_rate

        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._text = ""
        self._offset = 0
        self._endpoint = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str = ""

        self._thread = threading.Thread(target=self._run, name="volc-asr", daemon=True)
        self._thread.start()

    # ---------- 对上层 ----------
    def push(self, pcm: bytes) -> tuple[str, bool]:
        if self._stop.is_set():
            # 引擎已经关了（切换引擎时会有一两帧在飞），当空结果
            return "", False
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            log.warning("火山 ASR 发送队列已满，丢弃一帧音频")
        with self._lock:
            return self._text, False

    def reset(self) -> None:
        # 火山按句返回（result_type=single），所以重置就是清空当前句；
        # 之前用"偏移量截断累计文本"是错的，会把下一句的开头切掉。
        with self._lock:
            self._text = ""
            self._endpoint = False

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        log.info("火山 ASR 已关闭%s", f"（{self._error}）" if self._error else "")

    # ---------- 内部 ----------
    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "X-Api-App-Key": self._app_id or "0",
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }

    def _config_payload(self) -> bytes:
        config = {
            "user": {"uid": "interview-bridge"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": self._sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
                "result_type": "single",
            },
        }
        return json.dumps(config, ensure_ascii=False).encode("utf-8")

    def _run(self) -> None:
        try:
            with connect(
                self._url,
                additional_headers=self._headers(),
                max_size=None,
                open_timeout=15,
                close_timeout=3,
            ) as socket:
                socket.send(build_frame(MSG_FULL_CLIENT, self._config_payload()))
                log.info("火山 ASR 已连接 (%s)", self._resource_id)

                while not self._stop.is_set():
                    self._drain_audio(socket)
                    try:
                        frame = socket.recv(timeout=0.05)
                    except TimeoutError:
                        continue
                    if frame:
                        self._handle(frame)

                socket.send(build_frame(MSG_AUDIO_ONLY, b"", serialization=SERIAL_NONE, compression=COMPRESS_NONE, flags=FLAG_LAST))
        except Exception as exc:  # noqa: BLE001 - 连接失败要能反馈到界面
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("火山 ASR 连接中断: %s", self._error, exc_info=True)

    def _drain_audio(self, socket) -> None:
        for _ in range(4):
            try:
                pcm = self._queue.get_nowait()
            except queue.Empty:
                return
            socket.send(
                build_frame(MSG_AUDIO_ONLY, pcm, serialization=SERIAL_NONE, compression=COMPRESS_NONE)
            )

    def _handle(self, frame: bytes) -> None:
        parsed = parse_frame(frame)
        kind = parsed["type"]
        payload = parsed["payload"]

        if kind == MSG_ERROR:
            self._error = json.dumps(payload, ensure_ascii=False)[:200]
            log.warning("火山 ASR 错误: %s", self._error)
            return
        if kind != MSG_FULL_SERVER or not isinstance(payload, dict):
            return

        result = payload.get("result")
        text = ""
        if isinstance(result, dict):
            text = str(result.get("text") or "")
        elif isinstance(result, list):
            text = "".join(str(item.get("text") or "") for item in result if isinstance(item, dict))

        if text:
            with self._lock:
                # 仅去掉句首可能残留的标点，不做前缀截断
                self._text = text.lstrip("？?。，、, ")
