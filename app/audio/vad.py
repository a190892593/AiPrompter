"""能量 VAD：判断"现在有没有人在说话"和"这句话说完了没有"。"""

from __future__ import annotations


class EnergyVAD:
    def __init__(self, threshold: float, silence_ms: int, frame_ms: int) -> None:
        self.threshold = threshold
        self.silence_frames = max(1, int(round(silence_ms / max(1, frame_ms))))
        self._silent_run = 0
        self._had_speech = False

    def push(self, rms: float) -> tuple[bool, bool]:
        """返回 (是否在说话, 是否该断句)。"""
        if rms >= self.threshold:
            self._silent_run = 0
            self._had_speech = True
            return True, False

        self._silent_run += 1
        if self._had_speech and self._silent_run >= self.silence_frames:
            self._had_speech = False
            self._silent_run = 0
            return False, True
        return False, False

    def reset(self) -> None:
        self._silent_run = 0
        self._had_speech = False
