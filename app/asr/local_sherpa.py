"""本地离线识别：sherpa-onnx 流式模型（paraformer / zipformer）。

完全在本机 CPU 上跑，音频不出机器，适合保密面试与断网环境。
模型文件放在 backend/models/<model-name>/，缺失时给出明确安装指引。

识别器（模型权重）是进程级共享的，每个音频通道各持有一个 stream，
这样两条通道并发识别不会互相污染，也不会重复加载几百 MB 的模型。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from ..config import settings

log = logging.getLogger("interview.asr.local")

SAMPLE_RATE = 16000

_RECOGNIZER: Any = None
_LOCK = threading.Lock()
# sherpa-onnx 的 recognizer 内部非线程安全：两条通道（面试官/候选人）并发 push 会让
# 特征缓冲越界（features.cc: GetFrames 崩溃）。这里串行化 push/reset。
_PUSH_LOCK = threading.Lock()


def _find(model_dir: Path, *names: str) -> Path | None:
    for name in names:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    return None


def _pick(model_dir: Path, prefix: str) -> Path | None:
    """按前缀找 onnx 文件（不同模型文件名差异很大，优先 int8 体积小）。"""
    candidates = sorted(model_dir.glob(f"{prefix}*.onnx"))
    if not candidates:
        return None
    int8 = [c for c in candidates if "int8" in c.name]
    return int8[0] if int8 else candidates[0]


def resolve_model_dir() -> Path:
    configured = settings.asr_model_dir
    # 注意：Path("") 会变成 "."，直接 is_dir() 会误判成当前目录
    if str(configured) not in ("", ".") and configured.is_dir():
        return configured
    base = settings.models_dir
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "tokens.txt").is_file():
                return child
    raise FileNotFoundError(
        f"没有找到本地 ASR 模型。请运行 `python tools/download_models.py`，"
        f"模型会下载到 {settings.models_dir}"
    )


def available() -> tuple[bool, str]:
    """模型是否就绪，用于前端显示通道状态。"""
    try:
        resolve_model_dir()
    except FileNotFoundError as exc:
        return False, str(exc)
    import importlib.util

    if importlib.util.find_spec("sherpa_onnx") is None:
        return False, "未安装 sherpa-onnx，请 pip install sherpa-onnx"
    return True, "ok"


def get_recognizer() -> Any:
    global _RECOGNIZER
    if _RECOGNIZER is not None:
        return _RECOGNIZER

    with _LOCK:
        if _RECOGNIZER is not None:
            return _RECOGNIZER

        import sherpa_onnx

        directory = resolve_model_dir()
        tokens = _find(directory, "tokens.txt")
        encoder = _pick(directory, "encoder")
        decoder = _pick(directory, "decoder")
        joiner = _pick(directory, "joiner")
        if tokens is None or encoder is None or decoder is None:
            raise FileNotFoundError(f"模型目录文件不全: {directory}")

        common = {
            "tokens": str(tokens),
            "encoder": str(encoder),
            "decoder": str(decoder),
            "num_threads": 2,
            "sample_rate": SAMPLE_RATE,
            "feature_dim": 80,
            "decoding_method": "greedy_search",
            "enable_endpoint_detection": True,
            "rule1_min_trailing_silence": 2.4,
            "rule2_min_trailing_silence": 1.2,
            "rule3_min_utterance_length": 20,
        }

        if "paraformer" in directory.name.lower() or "paraformer" in encoder.name:
            recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(**common)
            engine_name = "local:paraformer"
        else:
            if joiner is None:
                raise FileNotFoundError(f"transducer 模型缺少 joiner: {directory}")
            recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(joiner=str(joiner), **common)
            engine_name = "local:zipformer"

        _RECOGNIZER = recognizer
        log.info("本地识别模型已加载: %s (%s)", engine_name, directory.name)

    return _RECOGNIZER


class LocalEngine:
    def __init__(self) -> None:
        self._recognizer = get_recognizer()
        self._stream = self._recognizer.create_stream()
        self.name = "local"

    def push(self, pcm: bytes) -> tuple[str, bool]:
        # 切换引擎时旧实例会被关掉，而通道可能还有一帧在飞：直接当空结果，不要抛异常
        if self._stream is None:
            return "", False
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return "", False
        # 串行化：recognizer 共享且非线程安全，两通道并发 push 会崩
        with _PUSH_LOCK:
            if self._stream is None:
                return "", False
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)

            result = self._recognizer.get_result(self._stream)
            text = (result if isinstance(result, str) else getattr(result, "text", "")).strip()
            endpoint = bool(self._recognizer.is_endpoint(self._stream))
        return text, endpoint

    def reset(self) -> None:
        if self._stream is None:
            return
        with _PUSH_LOCK:
            if self._stream is None:
                return
            try:
                self._recognizer.reset(self._stream)
            except Exception:  # noqa: BLE001 - 不同版本 API 名称略有差异
                self._stream = self._recognizer.create_stream()

    def close(self) -> None:
        self._stream = None  # type: ignore[assignment]
