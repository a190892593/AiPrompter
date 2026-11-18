"""云端识别：OpenAI 兼容的 /audio/transcriptions 分段转写。

大多数云厂商都提供这一协议的音频转写接口（SiliconFlow、Groq、OpenAI、
以及各家网关）。做法是：按静音切句，句子结束后整段上传转写，
说话过程中用滚动缓冲做"伪流式"上屏，兼顾效果与成本。

对比原生 WebSocket 流式 ASR（火山/阿里云），这种方式接入成本最低、
同一条链路能换任意厂商；代价是首字延迟高 300–600ms。需要极限低延迟时
可再实现原生流式适配器，接口与本类一致即可平滑替换。
"""

from __future__ import annotations

import io
import logging
import wave

import httpx

from ..config import settings

log = logging.getLogger("interview.asr.cloud")

SAMPLE_RATE = 16000


def _to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


class CloudEngine:
    """分段上传：每 segment_seconds 秒尝试转写一次，静音后定稿。"""

    def __init__(self, min_partial_interval: float = 1.0, silence_final: float = 0.7) -> None:
        if not settings.cloud_asr_key:
            raise RuntimeError("未配置 CLOUD_ASR_KEY，无法使用云端识别")
        self.name = f"cloud:{settings.cloud_asr_model}"
        self._client = httpx.Client(
            base_url=settings.cloud_asr_base_url,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"Authorization": f"Bearer {settings.cloud_asr_key}"},
        )
        self._buffer = bytearray()
        self._last_partial = 0.0
        self._last_growth = 0.0
        self._text = ""
        self._min_partial = min_partial_interval
        self._silence_final = silence_final
        self._clock = 0.0

    # ---- 内部 ----
    def _transcribe(self, pcm: bytes) -> str:
        files = {"file": ("audio.wav", _to_wav(pcm), "audio/wav")}
        data = {"model": settings.cloud_asr_model, "language": settings.cloud_asr_language}
        try:
            resp = self._client.post("/audio/transcriptions", files=files, data=data)
            resp.raise_for_status()
            return str(resp.json().get("text") or "").strip()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("云端转写失败: %s", exc)
            return self._text

    # ---- 接口 ----
    def push(self, pcm: bytes) -> tuple[str, bool]:
        dt = len(pcm) / 2 / SAMPLE_RATE
        self._clock += dt
        self._buffer.extend(pcm)
        self._last_growth = self._clock

        silent = all(abs(int.from_bytes(pcm[i : i + 2], "little", signed=True)) < 320
                     for i in range(0, len(pcm) - 1, 2))

        if silent and self._clock - self._last_growth > self._silence_final and len(self._buffer) > SAMPLE_RATE // 2:
            self._text = self._transcribe(bytes(self._buffer))
            self._buffer.clear()
            final = self._text
            self._text = ""
            return final, True

        if self._clock - self._last_partial >= self._min_partial and len(self._buffer) > SAMPLE_RATE // 2:
            self._last_partial = self._clock
            self._text = self._transcribe(bytes(self._buffer))
        return self._text, False

    def reset(self) -> None:
        self._buffer.clear()
        self._text = ""
        self._last_partial = self._clock

    def close(self) -> None:
        self._client.close()
