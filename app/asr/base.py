"""语音识别引擎抽象：统一"喂 PCM、取文本"的接口，云端与本地可热切换。"""

from __future__ import annotations

from typing import Protocol


class ASREngine(Protocol):
    """所有引擎都按 16kHz / 单声道 / int16 小端 PCM 接收音频。"""

    name: str

    def push(self, pcm: bytes) -> tuple[str, bool]:
        """喂一段音频，返回（当前这句话的累计文本, 是否已检测到句末）。

        同步实现即可，调用方会用线程池包一层，避免阻塞事件循环。
        """
        ...

    def reset(self) -> None:
        """一句话结束后重置，准备接收下一句。"""
        ...

    def close(self) -> None:
        ...
