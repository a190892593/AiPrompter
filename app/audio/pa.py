"""全局唯一的 PyAudio 实例。

两个坑都踩过：
1. 每个采集线程各自 `PyAudio()` 再 `terminate()`，会让 PortAudio 的全局初始化计数错乱，
   Windows 上直接原生崩溃（进程无 traceback 退出）→ 所以这里全进程单例。
2. 单例会把**设备列表**一起缓存住。蓝牙耳机重连、USB 声卡插拔之后，设备索引变了，
   还拿旧索引去开流就会报 `-9999 Unanticipated host error` → 所以提供 refresh()，
   在空闲（没有活动流）时重建实例，让设备列表保持最新。
"""

from __future__ import annotations

import logging
import threading
import time

import pyaudiowpatch as pyaudio

log = logging.getLogger("audio.pa")

_lock = threading.RLock()
_instance: pyaudio.PyAudio | None = None
_streams = 0
_retired: list = []
_last_refresh = 0.0
_refresh_warned = False


def get_audio() -> pyaudio.PyAudio:
    global _instance
    with _lock:
        if _instance is None:
            _instance = pyaudio.PyAudio()
        return _instance


def stream_opened() -> None:
    global _streams
    with _lock:
        _streams += 1


def stream_closed() -> None:
    global _streams
    with _lock:
        _streams = max(0, _streams - 1)


def refresh_audio() -> bool:
    """禁用 PortAudio 重建，返回 False。

    原因：Windows 上 terminate + 重新 init（甚至只是再建一个实例）都会随机触发
    access violation，整个服务进程直接消失、不留 traceback。设备列表因此在进程
    生命周期内只初始化一次；拔插设备/蓝牙重连后需要重启应用才能刷新。
    """
    global _refresh_warned
    with _lock:
        if not _refresh_warned:
            _refresh_warned = True
            log.warning("已跳过设备列表刷新（稳定性保护）；如需刷新设备请重启应用")
    return False
