"""DeepSeek 官方语音识别（`POST /v1/tools/asr`）。

官方接口是**非流式**的：一次请求发完整音频，返回整段转写，上限 120 秒 / 8 MiB。
所以这里按「静音切句」工作：一边攒 16k 单声道 PCM，一边用一个轻量能量检测
判断这句话说完没有；说完就把整段包成 WAV 上传，拿到文本后当作句末定稿。

计费按音频时长（0.39 元/小时，限时五折），所以**不做滚动 partial**：
每一句只上传一次，避免同一段音频重复计费。代价是说话过程中没有"边说边出字"，
说完 1 秒左右整句一起上屏。
"""

from __future__ import annotations

import io
import logging
import math
import struct
import wave
from concurrent.futures import Future, ThreadPoolExecutor

import httpx

from ..config import settings

log = logging.getLogger("interview.asr.deepseek")

SAMPLE_RATE = 16000
# 官方上限 120 秒，留点余量；超过就强制切一句先发走
MAX_SECONDS = 116.0
MIN_SECONDS = 0.45


def _to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    count = len(pcm) // 2
    total = 0
    for (sample,) in struct.iter_unpack("<h", pcm[: count * 2]):
        total += sample * sample
    return math.sqrt(total / count) / 32768.0


class DeepSeekASREngine:
    """整段上传式识别；接口与其它引擎一致（push/reset/close）。"""

    name = "deepseek"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        silence_final: float = 0.8,
        threshold: float | None = None,
    ) -> None:
        key = (api_key or settings.deepseek_asr_key).strip()
        if not key:
            raise RuntimeError("未配置 DeepSeek API Key（LLM_API_KEY / DEEPSEEK_ASR_KEY）")
        base = (base_url or settings.deepseek_asr_base_url).rstrip("/")
        # httpx 的 base_url 拼接：带 "/v1" 不带尾斜杠时会被相对路径覆盖掉，这里统一补斜杠
        self._client = httpx.Client(
            base_url=f"{base}/",
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {key}"},
        )
        self._silence_final = silence_final
        self._threshold = settings.vad_threshold if threshold is None else threshold
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-asr")
        self._reset_state()

    # ---- 内部 ----
    def _reset_state(self) -> None:
        self._buffer = bytearray()
        self._clock = 0.0
        self._last_speech = 0.0
        self._had_speech = False
        self._future: Future[str] | None = None

    def _transcribe(self, pcm: bytes) -> str:
        files = {"file": ("audio.wav", _to_wav(pcm), "audio/wav")}
        try:
            resp = self._client.post("tools/asr", files=files)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("DeepSeek ASR 转写失败: %s", exc)
            return ""
        text = str(data.get("text") or "").strip()
        log.info(
            "DeepSeek ASR: %.1fs 音频 -> %d 字（request_id=%s）",
            len(pcm) / 2 / SAMPLE_RATE,
            len(text),
            data.get("request_id", ""),
        )
        return text

    # ---- 接口 ----
    def push(self, pcm: bytes) -> tuple[str, bool]:
        # 上一句的上传还没回来：先不攒新音频，等结果
        if self._future is not None:
            if not self._future.done():
                return "", False
            text = ""
            try:
                text = self._future.result()
            except Exception:  # noqa: BLE001 - 单句失败不能拖垮通道
                log.exception("DeepSeek ASR 任务异常")
            self._future = None
            if text:
                return text, True
            return "", False

        dt = len(pcm) / 2 / SAMPLE_RATE
        self._clock += dt
        self._buffer.extend(pcm)
        if _rms(pcm) > self._threshold:
            self._had_speech = True
            self._last_speech = self._clock

        duration = len(self._buffer) / 2 / SAMPLE_RATE
        silent_for = self._clock - self._last_speech
        ready = (
            self._had_speech
            and duration >= MIN_SECONDS
            and (silent_for >= self._silence_final or duration >= MAX_SECONDS)
        )
        if ready:
            pcm_out = bytes(self._buffer)
            self._buffer.clear()
            self._had_speech = False
            self._future = self._executor.submit(self._transcribe, pcm_out)
        return "", False

    def reset(self) -> None:
        self._reset_state()

    def close(self) -> None:
        self._reset_state()
        self._executor.shutdown(wait=False)
        self._client.close()
