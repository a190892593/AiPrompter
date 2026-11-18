"""本地桥配置。所有项都可用环境变量或 .env 覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

load_dotenv(PROJECT_DIR / ".env")
load_dotenv(APP_DIR / ".env")


class Settings:
    # ---- 服务 ----
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8765"))

    # ---- 音频 ----
    target_rate: int = 16000
    frame_ms: int = 30
    # 能量门限：低于它视为静音（安静房间底噪约 0.001-0.003）
    vad_threshold: float = float(os.getenv("VAD_THRESHOLD", "0.008"))
    # 连续静音多久算一句话说完（默认 900ms：句子中间的停顿不该把话切断）
    vad_silence_ms: int = int(os.getenv("VAD_SILENCE_MS", "900"))
    # 同一段话被 VAD/端点切碎后，多少秒内的下一段继续拼回同一条转写（0=不合并）
    transcript_merge_gap: float = float(os.getenv("TRANSCRIPT_MERGE_GAP", "15"))
    # 定稿去重：同通道多少秒内高度重合的定稿不再新增气泡（0=关闭去重）
    transcript_dedup_seconds: float = float(os.getenv("TRANSCRIPT_DEDUP_SECONDS", "30"))
    # 回声抑制：候选人通道在多少秒内听到和面试官同样的话，就判定为扬声器回声（0=关闭）
    transcript_echo_seconds: float = float(os.getenv("TRANSCRIPT_ECHO_SECONDS", "30"))
    # 回声判定的相似度门槛（0~1，越大越保守）
    transcript_echo_similarity: float = float(os.getenv("TRANSCRIPT_ECHO_SIMILARITY", "0.8"))
    # 缓冲多久的音频，满了丢最旧
    buffer_seconds: float = float(os.getenv("BUFFER_SECONDS", "2.0"))

    # ---- 仿真 ----
    # 一场仿真最多问多少题（避免无限出题浪费 token）
    sim_question_limit: int = int(os.getenv("SIM_QUESTION_LIMIT", "20"))

    # ---- 大模型 ----
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "").strip()
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    # 话术生成单独指定模型（留空则与意图识别同模型）
    llm_answer_model: str = os.getenv("LLM_ANSWER_MODEL", "").strip()
    # 是否记录每次调用的出入参（用于排查问题，关闭可省磁盘）
    llm_log_enabled: bool = os.getenv("LLM_LOG", "1") not in ("0", "false", "False")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    # 思考模式：调用 DeepSeek 原生 thinking 参数（不再单独切推理模型）
    llm_thinking: bool = os.getenv("LLM_THINKING", "0") not in ("0", "false", "False")
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "90"))

    # ---- ASR ----
    # auto | local | cloud | mock
    asr_provider: str = os.getenv("ASR_PROVIDER", "auto").strip().lower()
    models_dir: Path = Path(os.getenv("MODELS_DIR", str(PROJECT_DIR / "models")))
    asr_model_dir: Path = Path(os.getenv("ASR_MODEL_DIR", ""))
    cloud_asr_base_url: str = os.getenv("CLOUD_ASR_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
    cloud_asr_key: str = os.getenv("CLOUD_ASR_KEY", "").strip()
    cloud_asr_model: str = os.getenv("CLOUD_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
    cloud_asr_language: str = os.getenv("CLOUD_ASR_LANGUAGE", "zh")

    # ---- 语音识别：DeepSeek 官方 ASR（/v1/tools/asr，非流式）----
    # 没单独配 key 时复用大模型那把 key（同一个 DeepSeek 账号）
    deepseek_asr_key: str = os.getenv("DEEPSEEK_ASR_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    deepseek_asr_base_url: str = (
        os.getenv("DEEPSEEK_ASR_BASE_URL", "").strip()
        or os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    ).rstrip("/")

    # ---- 语音识别：火山豆包流式语音识别 2.0 ----
    volc_speech_key: str = os.getenv("VOLC_SPEECH_KEY", "").strip()
    volc_app_id: str = os.getenv("VOLC_APP_ID", "").strip()
    volc_resource_id: str = os.getenv("VOLC_RESOURCE_ID", "volc.seedasr.sauc.duration")
    volc_speech_url: str = os.getenv(
        "VOLC_SPEECH_URL", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )

    # ---- 意图识别 ----
    judge_confidence: float = float(os.getenv("JUDGE_CONFIDENCE", "0.6"))
    judge_debounce_ms: int = int(os.getenv("JUDGE_DEBOUNCE_MS", "900"))
    # 意图识别二次校验：低置信/合并多句/被纠偏过的问题，再让模型复核一遍，宁可不出卡也不出错题
    intent_verify: bool = os.getenv("INTENT_VERIFY", "1") not in ("0", "false", "False")
    intent_verify_below: float = float(os.getenv("INTENT_VERIFY_BELOW", "0.85"))
    intent_verify_timeout: float = float(os.getenv("INTENT_VERIFY_TIMEOUT", "8"))
    # 自动联网搜索：标准档和深度档自动启用 DeepSeek 原生联网；极简档不联网
    answer_web_search: bool = os.getenv("ANSWER_WEB_SEARCH", "1") not in ("0", "false", "False")
    # 评分报告是否开思考（报告允许慢，思考有助于点评质量）
    report_thinking: bool = os.getenv("REPORT_THINKING", "1") not in ("0", "false", "False")
    # 意图识别是否开思考（默认关；仅用于效果对比，开后会明显变慢）
    judge_thinking: bool = os.getenv("JUDGE_THINKING", "0") not in ("0", "false", "False")
    # 投机意图识别：用 ASR 半句提前预判，定稿命中就复用（担心准确性可设 0 关闭）
    speculative_intent: bool = os.getenv("SPECULATIVE_INTENT", "0") not in ("0", "false", "False")
    # 单次意图识别最长等多久，超时用规则兜底（避免一次网络抖动卡住整场）
    judge_timeout: float = float(os.getenv("JUDGE_TIMEOUT", "20"))
    answer_level: str = os.getenv("ANSWER_LEVEL", "standard")

    # ---- 仿真语音 ----
    # auto：先本机 SAPI，失败回退 Edge 在线；sapi：只用本机；edge：只用在线
    tts_engine: str = os.getenv("TTS_ENGINE", "auto").strip().lower()

    # ---- 存储 ----
    data_dir: Path = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data")))

    # ---- 日志 ----
    # 关键操作都会写进 data/logs/app.log，排查问题先看它
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    log_file: bool = os.getenv("LOG_FILE", "1").strip().lower() not in ("0", "false", "no", "off")
    log_dir: Path = Path(os.getenv("LOG_DIR") or (PROJECT_DIR / "data" / "logs"))

    @property
    def log_path(self) -> Path:
        return self.log_dir / "app.log"

    @property
    def llm_live(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def answer_model(self) -> str:
        """话术专用模型；没配就跟着意图识别模型走。"""
        return self.llm_answer_model or self.llm_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "interview.db"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
