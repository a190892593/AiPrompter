"""本地 TTS（Windows SAPI）：把文字合成 wav，用于仿真回放。

不引入额外依赖，直接调用系统自带的 System.Speech；结果按文本哈希缓存，
同一句话只合成一次。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from .config import settings

log = logging.getLogger("tts")

CACHE_DIR = settings.data_dir / "tts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$text = Get-Content -LiteralPath {text_path} -Raw -Encoding UTF8
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.SelectVoice({voice}) } catch { }
$synth.Rate = 0
$synth.SetOutputToWaveFile({out_path})
$synth.Speak($text)
$synth.Dispose()
"""


def sanitize(text: str) -> str:
    """去掉话术里的占位符，避免 TTS 把「【XX万单】」念成"左括号XX万单右括号"。"""
    import re

    text = re.sub(r"[【\[]([^】\]]*)[】\]]", r"\1", text)
    text = re.sub(r"[Xx]{2,}|[\dXx]+[万千百]?单", "一些", text)
    text = text.replace("X", "").replace("x", "")
    return re.sub(r"\s+", "", text).strip()


def play_blocking(path: Path) -> None:
    """把 wav 播到系统默认扬声器，播完才返回（仅 Windows）。"""
    try:
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    except Exception:  # noqa: BLE001 - 播放失败不影响仿真
        import logging

        logging.getLogger("tts").warning("播放音频失败", exc_info=True)


def _resolve_playback_device(name: str) -> tuple[int | None, int]:
    """把"环回设备名"映射到真正的播放设备，返回 (设备索引, 采样率)。"""
    try:
        import pyaudiowpatch as pyaudio

        from .audio.pa import get_audio, stream_closed, stream_opened

        pa = get_audio()
        wanted = name.replace(" [Loopback]", "").strip()
        try:
            devices = pa.get_device_info_generator()
        except Exception:  # noqa: BLE001
            return None, 44100

        best: tuple[int, int] | None = None
        fallback: tuple[int, int] | None = None
        for device in devices:
            if int(device.get("maxOutputChannels", 0)) <= 0:
                continue
            rate = int(device.get("defaultSampleRate", 44100))
            info = (int(device["index"]), rate)
            if fallback is None:
                fallback = info
            if wanted and wanted in str(device.get("name", "")) and "[Loopback]" not in str(device.get("name", "")):
                best = info
                break
        return best or fallback or (None, 44100)
    except Exception:  # noqa: BLE001
        return None, 44100


def play_with_device(wav_path: Path, device_name: str = "") -> bool:
    """优先播到指定设备（会议扬声器），拿不到设备就用系统默认。返回是否成功。"""
    try:
        import numpy as np
        import pyaudiowpatch as pyaudio

        from .audio.pa import get_audio, stream_closed, stream_opened
        from .audio.resample import Resampler, to_int16

        import wave

        with wave.open(str(wav_path), "rb") as handle:
            frames = handle.getnframes()
            channels = handle.getnchannels()
            rate = handle.getframerate()
            raw = handle.readframes(frames)

        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)

        index, target_rate = _resolve_playback_device(device_name)
        if index is None:
            raise RuntimeError("没有可用的播放设备")
        if rate != target_rate:
            data = Resampler(rate, target_rate).process(data)
        pcm = to_int16(data)

        pa = get_audio()
        # 必须登记引用：否则设备刷新会在播放中途 terminate PortAudio，导致堆损坏崩溃
        stream_opened()
        try:
            with pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=target_rate,
                output=True,
                output_device_index=index,
                frames_per_buffer=2048,
            ) as stream:
                payload = pcm if isinstance(pcm, (bytes, bytearray)) else pcm.tobytes()
                stream.write(payload)
        finally:
            stream_closed()
        return True
    except Exception as exc:  # noqa: BLE001 - 播放失败不影响仿真
        log.warning("按指定设备播放失败，退回系统默认: %s", exc)
        try:
            play_blocking(wav_path)
            return True
        except Exception:  # noqa: BLE001
            return False


def self_check(text: str = "这是语音合成测试") -> dict:
    """自检：合成 + 播放，返回每一步结果，供界面"试听"用。"""
    result: dict = {"ok": False, "text": text}
    try:
        wav = synthesize_with_fallback(text)
        result["synth_bytes"] = wav.stat().st_size
        result["synth_ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["synth_ok"] = False
        result["error"] = str(exc)[:300]
        return result
    result["played"] = play_with_device(wav)
    result["engine"] = last_engine
    result["ok"] = bool(result["played"])
    return result


def wav_to_pcm16(path: Path, target_rate: int = 16000) -> bytes:
    """把 wav 读成 16k / 16bit / 单声道 PCM，供注入通道使用。"""
    import wave

    import numpy as np

    from .audio.resample import Resampler, to_int16

    if not path.is_file() or path.stat().st_size <= 1024:
        raise RuntimeError(f"音频文件不可用（{path.name} 为空或损坏）")

    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(frames)

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return to_int16(Resampler(rate, target_rate).process(data))


_sapi_broken = False
last_engine = ""


def _synth_sapi(text: str, voice: str = "Microsoft Huihui Desktop", rate: int = 3) -> Path:
    text = sanitize(text)
    if not text:
        raise ValueError("文本为空")

    digest = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:16]
    target = CACHE_DIR / f"{digest}.wav"
    if target.is_file() and target.stat().st_size > 1024:
        return target

    # 坏缓存（0 字节或只有文件头）先删掉，否则每次都会拿到坏文件
    target.unlink(missing_ok=True)

    text_file = CACHE_DIR / f"{digest}.txt"
    text_file.write_text(text, encoding="utf-8")

    script = (
        "Add-Type -AssemblyName System.Speech; "
        f"$text = Get-Content -LiteralPath '{text_file}' -Raw -Encoding UTF8; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"try {{ $s.SelectVoice('{voice}') }} catch {{ }}; "
        f"$s.Rate = {rate}; "
        f"$s.SetOutputToWaveFile('{target}'); "
        "$s.Speak($text); "
        "$s.Dispose()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    # SAPI 偶尔"成功返回但产物 0 字节"（音色不可用时 Speak 抛错但退出码仍为 0），
    # 必须按文件大小判断，否则会把空音频喂给识别器。
    size = target.stat().st_size if target.is_file() else 0
    if result.returncode != 0 or size <= 1024:
        target.unlink(missing_ok=True)
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(f"TTS 合成失败（产物 {size} 字节）: {detail[0][:160] if detail else '无输出'}")

    log.info("TTS 合成 %s 字 -> %s", len(text), target.name)
    return target

# 音色回退顺序：首选中文女声，失败再试其它已装音色
VOICE_FALLBACKS = ("Microsoft Huihui Desktop", "Microsoft Hanhan Desktop", "Microsoft Zira Desktop")


def synthesize_with_fallback(text: str, voice: str = "", rate: int = 3) -> Path:
    """按音色列表依次尝试，直到合成出可用音频。"""
    candidates = [voice] if voice else []
    candidates += [v for v in VOICE_FALLBACKS if v not in candidates]
    last: Exception | None = None
    for candidate in candidates:
        try:
            return synthesize(text, voice=candidate, rate=rate)
        except Exception as exc:  # noqa: BLE001 - 换下一个音色
            last = exc
    raise RuntimeError(f"所有音色合成失败: {last}")


# ---------------------------------------------------------------- Edge 在线语音

EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def _synth_edge(text: str) -> Path:
    """用 Edge 在线语音合成，不依赖系统 SAPI；结果解码成 16k 单声道 wav。"""
    import asyncio
    import wave

    digest = hashlib.sha1(f"edge|{EDGE_VOICE}|{text}".encode("utf-8")).hexdigest()[:16]
    target = CACHE_DIR / f"{digest}.wav"
    if target.is_file() and target.stat().st_size > 1024:
        return target

    mp3 = CACHE_DIR / f"{digest}.mp3"
    mp3.unlink(missing_ok=True)
    target.unlink(missing_ok=True)

    async def _run() -> None:
        import edge_tts

        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(str(mp3))

    asyncio.run(_run())
    if not mp3.is_file() or mp3.stat().st_size < 1024:
        raise RuntimeError("Edge 在线合成失败（返回空文件）")

    import miniaudio

    decoded = miniaudio.decode_file(
        str(mp3),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=16000,
    )
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(bytes(memoryview(decoded.samples)))

    mp3.unlink(missing_ok=True)
    log.info("Edge 合成 %s 字 -> %s", len(text), target.name)
    return target


def synthesize(text: str, voice: str = "Microsoft Huihui Desktop", rate: int = 3) -> Path:
    """合成语音。TTS_ENGINE 决定用哪个引擎：

    auto：先本机 SAPI（快、离线），失败后自动回退 Edge 在线语音
    sapi：只用本机，不出网（失败直接报错，方便定位）
    edge：只用在线，音质更好但需要联网
    """
    global _sapi_broken, last_engine
    text = sanitize(text)
    if not text:
        raise ValueError("文本为空")

    mode = (settings.tts_engine or "auto").strip().lower()

    if mode == "edge":
        wav = _synth_edge(text)
        last_engine = "edge"
        return wav

    if mode == "sapi":
        wav = _synth_sapi(text, voice=voice, rate=rate)
        last_engine = "sapi"
        return wav

    if not _sapi_broken:
        try:
            wav = _synth_sapi(text, voice=voice, rate=rate)
            last_engine = "sapi"
            return wav
        except Exception as exc:  # noqa: BLE001 - 记一次就够，别每次都重试
            _sapi_broken = True
            log.warning("本机 SAPI 不可用，改用 Edge 在线语音: %s", exc)

    wav = _synth_edge(text)
    last_engine = "edge"
    return wav
