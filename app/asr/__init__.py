"""识别引擎工厂：按配置与可用性挑选引擎，失败自动降级。"""

from __future__ import annotations

import logging
from typing import Literal

from ..config import settings
from .base import ASREngine
from .mock import CANDIDATE_SCRIPT, INTERVIEWER_SCRIPT, MockEngine

log = logging.getLogger("asr")

Channel = Literal["interviewer", "candidate"]
EngineKind = Literal["auto", "local", "cloud", "volc", "deepseek", "mock"]


def available_engines() -> dict[str, dict]:
    from . import local_sherpa

    local_ok, local_reason = local_sherpa.available()
    cloud_ok = bool(settings.cloud_asr_key)
    volc_ok = bool(settings.volc_speech_key)
    deepseek_ok = bool(settings.deepseek_asr_key)
    return {
        "deepseek": {
            "available": deepseek_ok,
            "reason": "ok" if deepseek_ok else "未配置 DeepSeek API Key",
            "label": "云端 · DeepSeek 官方 ASR（整段上传）",
        },
        "volc": {
            "available": volc_ok,
            "reason": "ok" if volc_ok else "未配置 VOLC_SPEECH_KEY",
            "label": "云端 · 火山豆包流式 2.0",
        },
        "local": {"available": local_ok, "reason": local_reason, "label": "本地 · sherpa-onnx paraformer"},
        "cloud": {
            "available": cloud_ok,
            "reason": "ok" if cloud_ok else "未配置 CLOUD_ASR_KEY",
            "label": f"云端 · {settings.cloud_asr_model}",
        },
        "mock": {"available": True, "reason": "ok", "label": "演示脚本（无需麦克风）"},
    }


def create(kind: EngineKind, channel: Channel) -> tuple[ASREngine, str]:
    """返回（引擎实例, 实际使用的类型）。任何失败都降级到 mock，保证不会白屏。"""
    if kind == "auto":
        # ASR_PROVIDER 指定了具体引擎就先用它；auto/none 时按本地 → 火山 → 云端顺序
        order: list[str] = []
        preferred = settings.asr_provider
        if preferred in ("local", "volc", "cloud", "deepseek"):
            order.append(preferred)
        for name in ("local", "volc", "cloud", "mock"):
            if name not in order:
                order.append(name)
    else:
        order = [kind, "mock"]

    last_error = ""
    for candidate in order:
        try:
            if candidate == "deepseek":
                from .deepseek_asr import DeepSeekASREngine

                engine = DeepSeekASREngine()
                log.info("识别引擎就绪: 通道=%s 类型=deepseek（官方 ASR，整段上传）", channel)
                return engine, "deepseek"
            if candidate == "volc":
                from .volc_speech import VolcSpeechEngine

                engine = VolcSpeechEngine(
                    api_key=settings.volc_speech_key,
                    app_id=settings.volc_app_id,
                    resource_id=settings.volc_resource_id,
                    url=settings.volc_speech_url,
                )
                log.info(
                    "识别引擎就绪: 通道=%s 类型=volc 资源=%s",
                    channel,
                    settings.volc_resource_id,
                )
                return engine, "volc"
            if candidate == "local":
                from .local_sherpa import LocalEngine

                engine = LocalEngine()
                log.info("识别引擎就绪: 通道=%s 类型=local（离线，音频不出本机）", channel)
                return engine, "local"
            if candidate == "cloud":
                from .cloud_openai import CloudEngine

                engine = CloudEngine()
                log.info("识别引擎就绪: 通道=%s 类型=cloud 模型=%s", channel, settings.cloud_asr_model)
                return engine, "cloud"
            script = INTERVIEWER_SCRIPT if channel == "interviewer" else CANDIDATE_SCRIPT
            log.warning("识别引擎就绪: 通道=%s 类型=mock（演示脚本，不是真实识别）", channel)
            return MockEngine(script), "mock"
        except Exception as exc:  # noqa: BLE001 - 逐级降级，记录原因即可
            last_error = f"{candidate}: {exc}"
            log.warning("识别引擎 %s 不可用（通道=%s）: %s", candidate, channel, exc)

    log.error("所有识别引擎均不可用，强制使用演示脚本: %s", last_error)
    script = INTERVIEWER_SCRIPT if channel == "interviewer" else CANDIDATE_SCRIPT
    return MockEngine(script), "mock"
