"""音频设备枚举与解析（Windows WASAPI）。

关键约定：
- 面试官的声音走 **环回（loopback）**：把"某个输出设备正在播放的声音"当录音设备读；
- 我的声音走 **麦克风输入**。

会议软件输出到哪个设备，就必须抓**那个设备**的环回。抓默认设备而会议走耳机，
结果就是一片静音——这是最常见的坑。
"""

from __future__ import annotations

import logging

import pyaudiowpatch as pyaudio

from .pa import get_audio, refresh_audio

log = logging.getLogger("audio.devices")


def enumerate_devices() -> dict:
    """返回给前端用的设备清单。空闲时先刷新设备列表，避免蓝牙重连后索引过期。"""
    refresh_audio()
    audio = get_audio()
    wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
    try:
        default_output = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    except Exception:  # noqa: BLE001
        default_output = ""

    outputs = []
    for device in audio.get_loopback_device_info_generator():
        outputs.append(
            {
                "id": device["name"],
                "name": device["name"],
                "rate": int(device["defaultSampleRate"]),
                "channels": int(device["maxInputChannels"]),
            }
        )

    try:
        default_input = audio.get_default_input_device_info()["name"]
    except Exception:  # noqa: BLE001
        default_input = ""

    inputs = []
    seen: set[str] = set()
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        name = info["name"]
        if info["maxInputChannels"] <= 0 or "Loopback" in name or name in seen:
            continue
        seen.add(name)
        inputs.append(
            {
                "id": name,
                "name": name,
                "rate": int(info["defaultSampleRate"]),
                "channels": int(info["maxInputChannels"]),
            }
        )

    return {
        "outputs": outputs,
        "inputs": inputs,
        "default_output": default_output,
        "default_input": default_input,
    }


def resolve_loopback(name: str | None) -> dict:
    """按名称找环回设备；找不到就退回默认输出设备的环回。"""
    refresh_audio()
    audio = get_audio()
    candidates = list(audio.get_loopback_device_info_generator())
    if not candidates:
        raise RuntimeError("这台机器没有可用的 WASAPI 环回设备")

    if name:
        for device in candidates:
            if device["name"] == name:
                return _normalize(device)
        for device in candidates:
            if name.lower() in device["name"].lower():
                return _normalize(device)
        raise RuntimeError(f"找不到环回设备：{name}")

    wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_out = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    base = default_out.split(" (")[0]
    for device in candidates:
        if base and base in device["name"]:
            return _normalize(device)
    return _normalize(candidates[0])


def resolve_input(name: str | None) -> dict:
    refresh_audio()
    audio = get_audio()
    if name:
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if info["name"] == name and info["maxInputChannels"] > 0 and "Loopback" not in info["name"]:
                return _normalize(info)
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if name.lower() in info["name"].lower() and info["maxInputChannels"] > 0:
                return _normalize(info)
        raise RuntimeError(f"找不到输入设备：{name}")

    try:
        return _normalize(audio.get_default_input_device_info())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("没有可用的麦克风") from exc


def _normalize(info: dict) -> dict:
    return {
        "index": int(info["index"]),
        "name": info["name"],
        "rate": int(info["defaultSampleRate"]),
        "channels": int(info["maxInputChannels"]),
    }
