"""流式重采样：任意采样率 → 16kHz 单声道 float32。

不做 HiFi，只求 ASR 听得清：
- 先做长度≈抽头比的移动平均当抗混叠低通（48k→16k 直接抽取会混叠）；
- 再用线性插值重采样；
- 保留上一块尾部样本，减少块边界爆音。
"""

from __future__ import annotations

import numpy as np

TARGET_RATE = 16000


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int = TARGET_RATE) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.ratio = self.src_rate / self.dst_rate
        self._taps = max(1, int(round(self.ratio)))
        self._carry = np.zeros(0, dtype=np.float32)

    @property
    def passthrough(self) -> bool:
        return abs(self.ratio - 1.0) < 1e-6

    def process(self, mono: np.ndarray) -> np.ndarray:
        if mono.size == 0:
            return np.zeros(0, dtype=np.float32)
        if self.passthrough:
            return mono.astype(np.float32, copy=False)

        data = np.concatenate([self._carry, mono.astype(np.float32)]) if self._carry.size else mono.astype(np.float32)

        if self._taps > 1 and data.size > self._taps:
            kernel = np.ones(self._taps, dtype=np.float32) / self._taps
            data = np.convolve(data, kernel, mode="same").astype(np.float32)

        out_len = int(data.size / self.ratio)
        if out_len <= 0:
            self._carry = data[-2:].copy() if data.size >= 2 else data.copy()
            return np.zeros(0, dtype=np.float32)

        positions = np.arange(out_len, dtype=np.float32) * self.ratio
        out = np.interp(positions, np.arange(data.size, dtype=np.float32), data).astype(np.float32)

        self._carry = data[-2:].copy() if data.size >= 2 else data.copy()
        return out


def to_int16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767).astype(np.int16).tobytes()
