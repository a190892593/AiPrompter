"""采集线程：把一个音频设备读成 16k 单声道 PCM。

用**回调模式**而不是阻塞 read()：
某些环回设备（尤其是当前没有音频会话的输出设备）会让 `read()` 永久阻塞，
而回调模式在没数据时只是不触发，不会把线程挂死。

打开失败会自动重试，并在重试前重建 PortAudio 实例 —— 蓝牙耳机重连、
USB 声卡插拔都会让设备索引变化，用旧索引开流必报 -9999。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

from .pa import get_audio, refresh_audio, stream_closed, stream_opened
from .resample import Resampler, to_int16

log = logging.getLogger("audio.capture")


class CaptureThread(threading.Thread):
    def __init__(self, device: dict, channel: str, frame_ms: int, sink, on_error=None) -> None:
        super().__init__(name=f"capture-{channel}", daemon=True)
        self.device = device
        self.channel = channel
        self.frame_ms = frame_ms
        self._sink = sink
        self._on_error = on_error
        self._stop_event = threading.Event()
        self._stream = None
        self._channels = max(1, min(2, int(device["channels"])))
        self._resampler = Resampler(int(device["rate"]))
        self.frames = 0
        self.last_frame_at = 0.0

    # ---- PortAudio 回调：必须快 ----
    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ANN001
        try:
            frames = np.frombuffer(in_data, dtype=np.float32)
            if self._channels > 1:
                frames = frames.reshape(-1, self._channels).mean(axis=1)
            if frames.size:
                rms = float(np.sqrt(np.mean(np.square(frames))))
                pcm = to_int16(self._resampler.process(frames))
                if pcm:
                    self.frames += 1
                    self.last_frame_at = time.monotonic()
                    self._sink(self.channel, pcm, rms)
        except Exception:  # noqa: BLE001 - 回调里绝不能抛
            log.exception("采集回调异常 %s", self.channel)
        return None, pyaudio.paContinue

    def _open_stream(self) -> None:
        rate = int(self.device["rate"])
        chunk = max(160, int(rate * self.frame_ms / 1000))
        last_error: Exception | None = None

        for attempt in range(4):
            if attempt > 0:
                # 设备列表可能已经变了（蓝牙重连/插拔），刷新后再用新索引
                refresh_audio()
                if self._stop_event.wait(0.3 * attempt):
                    break

            device = self.device
            if attempt > 0:
                try:
                    from .devices import resolve_input, resolve_loopback

                    resolver = resolve_loopback if self.channel == "interviewer" else resolve_input
                    device = resolver(device["name"])
                    self.device = device
                    rate = int(device["rate"])
                    # 设备可能换了（蓝牙重连/换声卡）：声道数和重采样器都要跟着换，
                    # 否则还按旧采样率重采样，出来的音频变调，识别全是错的
                    self._channels = max(1, min(2, int(device["channels"])))
                    self._resampler = Resampler(rate)
                    chunk = max(160, int(rate * self.frame_ms / 1000))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc

            try:
                self._stream = get_audio().open(
                    format=pyaudio.paFloat32,
                    channels=self._channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=chunk,
                    stream_callback=self._callback,
                )
                stream_opened()
                self._stream.start_stream()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("打开 %s 失败(第 %s 次): %s", self.channel, attempt + 1, exc)

        raise last_error or RuntimeError("无法打开音频设备")

    def run(self) -> None:
        started = time.monotonic()
        warned_silent = False
        failures = 0
        # 采集断开要能自己回来：蓝牙耳机休眠/重连、USB 声卡插拔都会让流挂掉。
        # 只要会话还在跑，就一直按 3 秒节奏重开；换过设备就按名字重新解析。
        while not self._stop_event.is_set():
            opened = False
            try:
                self._open_stream()
                opened = True
                failures = 0
                log.info(
                    "采集启动 %s <- %s (%s Hz, %s ch)",
                    self.channel,
                    self.device["name"],
                    self.device["rate"],
                    self._channels,
                )
                while not self._stop_event.is_set():
                    self._stop_event.wait(0.2)
                    # 环回设备最常见的坑：选错扬声器 / 设备被独占，流开着但一帧都没有
                    if not warned_silent and self.frames == 0 and time.monotonic() - started > 20:
                        warned_silent = True
                        log.warning(
                            "%s 通道 20 秒没收到任何音频帧：设备=%r 可能没在出声或被独占"
                            "（环回要选会议软件实际使用的那个扬声器）",
                            self.channel,
                            self.device.get("name"),
                        )
            except Exception as exc:  # noqa: BLE001 - 设备异常要上报，但不能让整场面试哑掉
                failures += 1
                # 每 20 次（约 1 分钟）才详细报一次，避免掉线时空刷日志和界面提示
                loud = failures == 1 or failures % 20 == 0
                if loud:
                    log.exception("采集失败 %s（第 %d 次，3 秒后重连）", self.channel, failures)
                    if self._on_error:
                        self._on_error(self.channel, exc)
                else:
                    log.warning("采集失败 %s（第 %d 次）: %s", self.channel, failures, exc)
            finally:
                try:
                    if self._stream is not None:
                        self._stream.stop_stream()
                        self._stream.close()
                except Exception:  # noqa: BLE001
                    pass
                self._stream = None
                if opened:
                    stream_closed()

            if self._stop_event.is_set():
                break
            self._stop_event.wait(3.0)

        log.info("采集结束 %s (共 %s 帧)", self.channel, self.frames)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=4.0)
