"""运行态：一次面试的采集、识别、意图识别编排。

一个 Session 同时管两条通道：
- interviewer：环回（扬声器）→ ASR → 意图识别 → 话术
- candidate ：麦克风 → ASR → 只上屏

线程与事件循环的分工：
- CaptureThread（线程）只把 PCM 塞进队列，用 call_soon_threadsafe 跨线程；
- 每个通道一个 asyncio 任务消费队列，ASR 的阻塞解码丢进 to_thread。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Awaitable, Callable

from . import asr, llmlog, store
from .asr.correct import correct_transcript
from .audio import CaptureThread, EnergyVAD, resolve_input, resolve_loopback
from .config import settings
from .pipeline import Pipeline
from .schemas import SessionConfig

log = logging.getLogger("state")

Broadcast = Callable[[dict], Awaitable[None]]

# ---- 定稿去重 ----
# 火山流式识别按它自己的句子边界回传累积文本，本地却按 VAD 静音切句。
# 两边不同步时，同一句话会被本地定稿一次、云端再重发一次，界面上就是"双份对话"。
_PUNCT_CHARS = set("，。！？、,.!?；;：:…—～~ \t\r\n　\"'“”‘’（）()【】[]")
REPEAT_SIMILARITY = 0.9


def _norm_text(text: str) -> str:
    """去掉标点、空白和引号，只留内容用于比较。"""
    return "".join(ch for ch in (text or "") if ch not in _PUNCT_CHARS)


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def classify_repeat(prev: str, text: str, gap: float, window: float) -> str:
    """判断新定稿和同通道上一条转写的关系。

    返回：
      keep    —— 新文本是上一条的重复或截断，直接丢弃
      replace —— 新文本覆盖并延长了上一条，原地替换，不新增气泡
      new     —— 与上一条无关，正常新增
    """
    if window <= 0 or gap > window:
        return "new"
    a, b = _norm_text(prev), _norm_text(text)
    if not a or not b:
        return "new"
    if a == b:
        return "keep"
    # 只有"明显更长"才算新增内容（多出 4 个字以上），否则视为同一句被重发
    longer = len(b) >= len(a) + 4
    # 一边完整包含另一边：保留更长的那份
    if b.startswith(a) or b.endswith(a):
        return "replace" if longer else "keep"
    if a.startswith(b) or a.endswith(b):
        return "keep"
    # 断句不同但内容几乎一样（"你没有" vs "你们没有"），或者同一段被重新识别
    short = min(len(a), len(b))
    if short >= 6 and _common_prefix_len(a, b) >= int(short * 0.8):
        return "replace" if longer else "keep"
    if SequenceMatcher(None, a, b).ratio() >= REPEAT_SIMILARITY:
        return "replace" if longer else "keep"
    return "new"


class Session:
    def __init__(self) -> None:
        self.active = False
        self.config: SessionConfig | None = None
        self.pipeline: Pipeline | None = None
        self.captures: dict[str, CaptureThread] = {}
        self.engines: dict[str, asr.ASREngine] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.tasks: list[asyncio.Task] = []
        self.levels: dict[str, float] = {"interviewer": 0.0, "candidate": 0.0}
        self.devices: dict[str, str] = {"interviewer": "", "candidate": ""}
        self.engine_kind = ""
        self.started_at = 0.0
        self.dropped = 0
        self.session_id = ""
        self.created_at = ""
        self.utterances: list[dict] = []
        # 每个通道最后一次定稿的文本：ASR 线程晚到的重复结果不再当作"实时识别"上屏
        self._last_final: dict[str, str] = {}
        self._dirty = False
        self._save_task: asyncio.Task | None = None
        self._injecting: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broadcast: Broadcast | None = None
        # 面试记录（一场面试一条）：status / started_at / ended_at / 岗位 / 简历
        self.meta: dict = {}

    # ---------- 生命周期 ----------
    async def start(
        self,
        cfg: SessionConfig,
        broadcast: Broadcast,
        interview: dict | None = None,
    ) -> dict:
        if self.active:
            raise RuntimeError("已经在采集中，请先停止")

        log.info(
            "会话启动中: 岗位=%r 级别=%s 引擎=%s JD=%d字 指定扬声器=%r 指定麦克风=%r",
            cfg.role,
            cfg.level,
            cfg.engine,
            len(cfg.background.strip()),
            cfg.output_device,
            cfg.input_device,
        )
        self._loop = asyncio.get_running_loop()
        self._broadcast = broadcast

        loopback = await asyncio.to_thread(resolve_loopback, cfg.output_device or None)
        mic = await asyncio.to_thread(resolve_input, cfg.input_device or None)
        interviewer_engine, kind_a = await asyncio.to_thread(asr.create, cfg.engine, "interviewer")
        candidate_engine, kind_b = await asyncio.to_thread(asr.create, cfg.engine, "candidate")

        self.config = cfg
        self.engines = {"interviewer": interviewer_engine, "candidate": candidate_engine}
        self.engine_kind = kind_a if kind_a == kind_b else f"{kind_a}/{kind_b}"
        self.devices = {"interviewer": loopback["name"], "candidate": mic["name"]}
        self.pipeline = Pipeline(cfg, broadcast)
        self.pipeline._transcript_hook = self.apply_transcript_correction
        self.session_id = uuid.uuid4().hex[:12]
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.utterances = []
        self._last_final = {}
        self.started_at = time.time()
        self.meta = {
            "status": "running",
            "started_at": self.started_at,
            "ended_at": None,
            "end_reason": "",
            "mode": "real",
            "position_id": None,
            "position_name": cfg.role,
            "resume": cfg.resume,
            "engines": [self.engine_kind],
        }
        # 接续一张已存在的面试记录：沿用它的 id、开始时间和元信息（计时不重来）
        if interview:
            self.session_id = str(interview.get("id") or self.session_id)
            self.created_at = str(interview.get("created_at") or self.created_at)
            started = float(interview.get("started_at") or self.started_at)
            self.started_at = started
            self.meta.update(
                {
                    "status": "running",
                    "started_at": started,
                    "ended_at": None,
                    "end_reason": "",
                    "mode": interview.get("mode", "real"),
                    "position_id": interview.get("position_id"),
                    "position_name": interview.get("position_name") or cfg.role,
                    "resume": interview.get("resume") or cfg.resume,
                }
            )
        self.dropped = 0
        self.levels = {"interviewer": 0.0, "candidate": 0.0}

        maxsize = max(8, int(settings.buffer_seconds * 1000 / settings.frame_ms))
        self.queues = {
            "interviewer": asyncio.Queue(maxsize=maxsize),
            "candidate": asyncio.Queue(maxsize=maxsize),
        }

        self.tasks = [
            asyncio.create_task(self._run_channel("interviewer")),
            asyncio.create_task(self._run_channel("candidate")),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._save_loop()),
        ]

        for channel, device in (("interviewer", loopback), ("candidate", mic)):
            capture = CaptureThread(
                device=device,
                channel=channel,
                frame_ms=settings.frame_ms,
                sink=self._make_sink(channel),
                on_error=self._make_error_handler(channel),
            )
            capture.start()
            self.captures[channel] = capture

        self.active = True
        # 本场后续所有模型调用都绑定到这场面试
        llmlog.set_interview(self.session_id)
        log.info(
            "会话已启动: id=%s 引擎=%s 面试官<-%s 我<-%s 队列容量=%d帧",
            self.session_id,
            self.engine_kind,
            loopback["name"],
            mic["name"],
            maxsize,
        )
        return self.snapshot()

    async def resume_from(self, doc: dict, broadcast: Broadcast) -> dict:
        """接续上一次会话：重新开采集，但沿用原会话 id、历史转写与卡片。"""
        if self.active:
            raise RuntimeError("已经在采集中，无需恢复")

        cfg_data = doc.get("config") or {}
        allowed = set(SessionConfig.model_fields)
        cfg = SessionConfig(**{k: v for k, v in cfg_data.items() if k in allowed})
        devices = doc.get("devices") or {}
        if devices:
            cfg = cfg.model_copy(
                update={
                    "output_device": devices.get("interviewer", "") or cfg.output_device,
                    "input_device": devices.get("candidate", "") or cfg.input_device,
                }
            )
        await self.start(cfg, broadcast, interview=doc)

        # 沿用原会话的历史转写与卡片，后续保存直接写回同一条记录
        self.utterances = list(doc.get("utterances") or [])
        # 续场时记住各通道最后一句，避免 ASR 回放历史文本又冒出"实时"气泡
        for item in reversed(self.utterances):
            channel = str(item.get("channel") or "")
            if channel and channel not in self._last_final:
                self._last_final[channel] = str(item.get("text") or "")
        questions = list(doc.get("questions") or [])
        if self.pipeline is not None:
            self.pipeline.qa = questions
            self.pipeline.history = [
                {"channel": u.get("channel", "interviewer"), "text": u.get("text", ""), "ts": u.get("ts", time.time())}
                for u in self.utterances[-200:]
            ]
            for question in questions[-5:]:
                if question.get("question"):
                    self.pipeline._recent.append((time.monotonic(), question["question"]))
            last = next((q for q in reversed(questions) if not q.get("closed")), None)
            self.pipeline._current = last or (questions[-1] if questions else None)
            if last is not None:
                last["closed"] = False
        log.info("会话恢复 %s：%s 句转写 / %s 张卡片", self.session_id, len(self.utterances), len(questions))
        return self.snapshot()

    async def switch_engine(self, kind: str) -> dict:
        """面试进行中切换识别引擎（本地 ↔ 云端），不重开会话。"""
        if not self.active:
            raise RuntimeError("面试没在运行，不能切换引擎")
        # 先建新引擎，失败了旧引擎继续用
        interviewer, kind_a = await asyncio.to_thread(asr.create, kind, "interviewer")
        try:
            candidate, kind_b = await asyncio.to_thread(asr.create, kind, "candidate")
        except Exception:
            await asyncio.to_thread(interviewer.close)
            raise
        # 请求了真实引擎却掉到演示脚本，等于识别废掉：宁可报错、保留当前引擎
        if kind != "mock" and "mock" in (kind_a, kind_b):
            for engine in (interviewer, candidate):
                try:
                    await asyncio.to_thread(engine.close)
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError(f"{kind} 引擎不可用，已保留当前识别引擎")

        old = self.engines
        self.engines = {"interviewer": interviewer, "candidate": candidate}
        self.engine_kind = kind_a if kind_a == kind_b else f"{kind_a}/{kind_b}"
        if self.config is not None:
            self.config = self.config.model_copy(update={"engine": kind})
        trail = list(self.meta.get("engines") or [])
        if not trail or trail[-1] != self.engine_kind:
            trail.append(self.engine_kind)
        self.meta["engines"] = trail
        # 通道循环每帧重新取 self.engines，这里等它换过来再关旧引擎，
        # 否则旧引擎会被"关着用"：volc 线程已停、本地模型 stream 已置空，识别会静默失效。
        await asyncio.sleep(0.2)
        for engine in old.values():
            try:
                await asyncio.to_thread(engine.close)
            except Exception:  # noqa: BLE001 - 旧引擎关不掉不影响新引擎
                pass
        await self._persist()
        log.info("识别引擎已切换: %s -> %s", kind, self.engine_kind)
        return self.snapshot()

    async def switch_devices(self, output_device: str = "", input_device: str = "") -> dict:
        """面试进行中切换扬声器环回/麦克风：只重启采集线程，会话和 ASR 不重开。"""
        if not self.active:
            raise RuntimeError("面试没在运行，不能切换设备")
        loopback = await asyncio.to_thread(
            resolve_loopback, output_device or self.devices.get("interviewer") or None
        )
        mic = await asyncio.to_thread(
            resolve_input, input_device or self.devices.get("candidate") or None
        )

        # 先停旧采集，再起新采集；队列和 ASR 引擎保持不动，对话不中断
        for capture in list(self.captures.values()):
            await asyncio.to_thread(capture.stop)
        self.captures.clear()
        for channel, device in (("interviewer", loopback), ("candidate", mic)):
            capture = CaptureThread(
                device=device,
                channel=channel,
                frame_ms=settings.frame_ms,
                sink=self._make_sink(channel),
                on_error=self._make_error_handler(channel),
            )
            capture.start()
            self.captures[channel] = capture

        self.devices = {"interviewer": loopback["name"], "candidate": mic["name"]}
        if self.config is not None:
            self.config = self.config.model_copy(
                update={"output_device": loopback["name"], "input_device": mic["name"]}
            )
        for engine in self.engines.values():
            try:
                await asyncio.to_thread(engine.reset)
            except Exception:  # noqa: BLE001 - 单个引擎 reset 失败不影响切换
                pass
        await self._persist()
        log.info(
            "设备已切换: 面试官<-%s 候选人<-%s",
            loopback["name"],
            mic["name"],
        )
        return self.snapshot()

    async def stop(self) -> dict:
        # 手动停止 = 本场结束；已经是「系统自动中断」的保留原状态
        if self.meta and self.meta.get("status") == "running":
            self.meta["status"] = "finished"
            self.meta["ended_at"] = time.time()
            self.meta["end_reason"] = "手动停止"
        await self._persist()

        for capture in self.captures.values():
            await asyncio.to_thread(capture.stop)
        self.captures.clear()

        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.tasks.clear()

        for engine in self.engines.values():
            try:
                await asyncio.to_thread(engine.close)
            except Exception:  # noqa: BLE001
                pass
        self.engines.clear()

        if self.pipeline is not None:
            await self.pipeline.close()

        self.active = False
        self.levels = {"interviewer": 0.0, "candidate": 0.0}
        log.info(
            "会话已停止: id=%s 转写=%d句 卡片=%d张 丢帧=%d 时长=%.0fs",
            self.session_id or "-",
            len(self.utterances),
            len(self.pipeline.qa) if self.pipeline else 0,
            self.dropped,
            (time.time() - self.started_at) if self.started_at else 0.0,
        )
        return self.snapshot()

    async def finish(self) -> dict:
        if self.pipeline is None:
            return {
                "overall_score": 0,
                "verdict": "还没有开始面试，没有可评分的内容",
                "dimensions": [],
                "per_question": [],
                "strengths": [],
                "gaps": [],
                "action_plan": ["先选择岗位并点「真实面试」或「仿真面试」，让系统听到面试内容"],
                "degraded": True,
            }
        return await self.pipeline.finish()

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "active": self.active,
            "engine": self.engine_kind,
            "devices": self.devices,
            "levels": self.levels,
            "dropped": self.dropped,
            "started_at": self.started_at,
            "turns": len(self.pipeline.qa) if self.pipeline else 0,
            "intent_stats": self.pipeline.stats_view() if self.pipeline else {},
            # 两路采集是否真的活着（掉线时前端能看出来）
            "captures": {
                channel: bool(thread.is_alive())
                for channel, thread in self.captures.items()
            },
            "interview": {
                "id": self.session_id,
                "status": self.meta.get("status", ""),
                "started_at": self.meta.get("started_at") or self.started_at,
                "ended_at": self.meta.get("ended_at"),
                "end_reason": self.meta.get("end_reason", ""),
                "role": self.meta.get("position_name", ""),
                "mode": self.meta.get("mode", "real"),
                "sim_enabled": bool(self.meta.get("sim_enabled", False)),
                "sim_mode": self.meta.get("sim_mode", ""),
                "resume": self.meta.get("resume", ""),
            },
        }

    def mark_auto_interrupted(self, ended_at: float) -> None:
        """超时自动中断：状态先落好，再走正常停止流程（停止不会覆盖它）。"""
        self.meta["status"] = "auto_interrupted"
        self.meta["ended_at"] = ended_at
        self.meta["end_reason"] = "超过 2 小时未手动结束，系统自动中断"
        log.warning("面试 %s 超时，标记系统自动中断", self.session_id)

    # ---------- 持久化 ----------
    async def broadcast_note(self, message: str) -> None:
        if self._broadcast is not None:
            await self._broadcast({"type": "note", "message": message})

    async def emit_final(self, channel: str, text: str) -> None:
        """兜底/仿真场景：直接产出一条最终转写，走和识别结束时一样的广播与流水线。"""
        text = correct_transcript((text or "").strip())
        if not text:
            return
        # 术语纠正交给意图识别模型（它会把误识别术语改对并回写这条转写），
        # 这里不再维护本地别名表
        speaker = "面试官" if channel == "interviewer" else "候选人"

        # 扬声器外放时，面试官的声音会被麦克风收进去，候选人通道会把同一句再转一遍。
        # 这种回声只污染上下文，判掉即可。
        if self._is_echo(channel, text):
            log.warning("跳过回声[%s]: %r", speaker, text[:120])
            return

        # 云端把同一句重新发一遍时不要新增气泡（否则界面上就是"双份对话"）。
        # 注意要回看同通道最近一条：中间插了对方的话也要能认出来。
        last = self._last_of(channel)
        if last is not None:
            gap = time.time() - float(last.get("ts") or 0)
            action = classify_repeat(
                str(last.get("text") or ""), text, gap, settings.transcript_dedup_seconds
            )
            if action == "keep":
                log.info("跳过重复定稿[%s]: %r", speaker, text[:120])
                self._last_final[channel] = str(last.get("text") or text)
                # 顺手广播一次 final_update：把可能残留的"实时"气泡刷回已定稿内容
                await self._broadcast(
                    {
                        "type": "final_update",
                        "channel": channel,
                        "text": self._last_final[channel],
                        "ts": time.time(),
                    }
                )
                return
            if action == "replace":
                last["text"] = text
                last["ts"] = time.time()
                self._last_final[channel] = text
                self._dirty = True
                log.info("定稿覆盖[%s]: %r", speaker, text[:120])
                await self._broadcast(
                    {"type": "final_update", "channel": channel, "text": text, "ts": time.time()}
                )
                if self.pipeline is not None:
                    await self.pipeline.feed_final(channel, text)
                return

        log.info("转写定稿[%s]: %r", speaker, text[:120])

        # 同一段话没说完被 VAD 切成两段：并回上一条气泡，而不是新增一块
        merged = self._merge_into_last(channel, text)
        if merged is not None:
            log.info("转写合并[%s]: %r", channel, merged[-120:])
            self._last_final[channel] = merged
            self._dirty = True
            await self._broadcast(
                {"type": "final_update", "channel": channel, "text": merged, "ts": time.time()}
            )
            if self.pipeline is not None:
                await self.pipeline.feed_final(channel, text)
            return

        self.record_final(channel, text)
        self._last_final[channel] = text
        await self._broadcast({"type": "final", "channel": channel, "text": text, "ts": time.time()})
        if self.pipeline is not None:
            await self.pipeline.feed_final(channel, text)

    _SENTENCE_END = tuple("。！？!?…；;.\n")

    def _is_echo(self, channel: str, text: str) -> bool:
        """候选人通道收到的内容，是不是刚播出来的面试官声音（扬声器回声）。

        判定：最近 `transcript_echo_seconds` 秒内面试官说过高度重合的话，
        且两段文本长度相近（避免把「您问的是 X」这种复述误判成回声）。
        """
        window = settings.transcript_echo_seconds
        if channel != "candidate" or window <= 0:
            return False
        b = _norm_text(text)
        if len(b) < 6:
            return False
        now = time.time()
        for item in reversed(self.utterances):
            if item.get("channel") != "interviewer":
                continue
            gap = now - float(item.get("ts") or 0)
            if gap > window:
                break
            a = _norm_text(str(item.get("text") or ""))
            if len(a) < 6:
                continue
            short, long_ = sorted((len(a), len(b)))
            if short / long_ < 0.5:
                continue
            # 一边完整包含另一边，且长度几乎一样：同一句被回声重发
            diff = long_ - short
            if (a in b or b in a) and diff <= max(3, int(short * 0.2)):
                return True
            if SequenceMatcher(None, a, b).ratio() >= settings.transcript_echo_similarity:
                return True
        return False

    def _last_of(self, channel: str) -> dict | None:
        """同通道最近一条转写（中间可能插过对方的话）。"""
        for item in reversed(self.utterances):
            if item.get("channel") == channel:
                return item
        return None

    def _merge_into_last(self, channel: str, text: str) -> str | None:
        """把这段转写并到上一句同通道转写上；不合适合并就返回 None。

        规则（只对候选人，避免打乱面试官意图识别节奏）：
        - 上一句同通道、时间间隔在 transcript_merge_gap 以内；
        - 上一句没有句末标点（说明话没说完）；
        - 合并后长度不超过限制。
        """
        if channel != "candidate" or settings.transcript_merge_gap <= 0:
            return None
        if not self.utterances:
            return None
        item = self.utterances[-1]
        # 中间如果有人插过话（比如面试官提问），就别跨着合并
        if item.get("channel") != channel:
            return None
        gap = time.time() - float(item.get("ts") or 0)
        prev = str(item.get("text") or "")
        if gap > settings.transcript_merge_gap or prev.endswith(self._SENTENCE_END):
            return None
        if len(prev) + len(text) > 600:
            return None
        joiner = " " if prev[-1:].isascii() and text[:1].isascii() else ""
        item["text"] = f"{prev}{joiner}{text}"
        item["ts"] = time.time()
        return item["text"]

    def apply_transcript_correction(self, channel: str, raw: str, corrected: str) -> None:
        """意图识别顺带纠正了整句转写：回写进会话记录（气泡由 pipeline 广播 final_update）。"""
        for item in reversed(self.utterances):
            if item.get("channel") != channel:
                continue
            if str(item.get("text") or "") == raw:
                item["text"] = corrected
                item["ts"] = time.time()
                self._dirty = True
            break

    def record_final(self, channel: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        log.debug("记录转写[%s]: %r", channel, text[:80])
        self.utterances.append(
            {"id": uuid.uuid4().hex[:8], "channel": channel, "text": text, "ts": time.time()}
        )
        if len(self.utterances) > 800:
            self.utterances = self.utterances[-400:]
        self._dirty = True

    def _doc(self) -> dict:
        doc = {
            "id": self.session_id,
            "created_at": self.created_at,
            "config": self.config.model_dump() if self.config else {},
            "utterances": self.utterances,
            "questions": self.pipeline.qa if self.pipeline else [],
            "intent_stats": self.pipeline.stats_view() if self.pipeline else {},
            "judge_log": self.pipeline.judge_log if self.pipeline else [],
            "report": self.pipeline._report if self.pipeline else None,
            "active": self.active,
            "engine": self.engine_kind,
            "devices": self.devices,
        }
        doc.update(self.meta)
        return doc

    async def _persist(self) -> None:
        if not self.session_id:
            return
        self._dirty = False
        try:
            await asyncio.to_thread(store.save, self._doc())
            log.debug(
                "会话已落盘: id=%s 转写=%d 卡片=%d",
                self.session_id,
                len(self.utterances),
                len(self.pipeline.qa) if self.pipeline else 0,
            )
        except Exception:  # noqa: BLE001 - 落盘失败不影响面试
            log.exception("保存会话失败: id=%s", self.session_id)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(1.5)
            if self.active or self._dirty:
                await self._persist()

    # ---------- 音频 → 队列 ----------
    async def feed_audio(self, channel: str, pcm16: bytes, chunk: int = 960, interval: float = 0.03) -> None:
        """把一段 PCM 按 30ms 节奏灌进指定通道，模拟"这个人在说话"。

        仿真回放用：绕开物理播放，但走的是同一条 采集队列 → ASR 的路径。
        """
        queue = self.queues.get(channel)
        if queue is None or not self.active:
            log.warning("注入音频被忽略: 通道=%s 会话未运行", channel)
            return
        log.info("注入音频: 通道=%s 时长=%.1fs", channel, len(pcm16) / 2 / 16000)
        import numpy as np

        # 注入期间屏蔽物理采集：否则真实静音帧会和注入语音交错进队列，
        # ASR 会看到"说-停-说"的碎片流，把一句话切碎。
        added = channel not in self._injecting
        if added:
            self._injecting.add(channel)
        try:
            for offset in range(0, len(pcm16), chunk):
                piece = pcm16[offset : offset + chunk]
                if not piece:
                    break
                samples = np.frombuffer(piece, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
                # 队列快满时先等消费端追上来，别把长回答切成一帧一个洞
                for _ in range(200):
                    if queue.qsize() < queue.maxsize - 2 or not self.active:
                        break
                    await asyncio.sleep(0.01)
                self._offer(queue, piece, rms)
                await asyncio.sleep(interval)
            # 注入结束不代表已经识别完：等队列消化干净再放开切句，
            # 否则残帧里的端点标记会把一整段回答切成两条转写
            for _ in range(300):
                if queue.qsize() == 0 or not self.active:
                    break
                await asyncio.sleep(0.05)
        finally:
            if added:
                self._injecting.discard(channel)

    @asynccontextmanager
    async def suppress_capture(self, *channels: str):
        """把指定通道的物理采集临时关掉（仿真播放声音时用，避免扬声器回灌）。"""
        added = [ch for ch in channels if ch not in self._injecting]
        for ch in added:
            self._injecting.add(ch)
        try:
            yield
        finally:
            for ch in added:
                self._injecting.discard(ch)

    def _make_sink(self, channel: str):
        loop = self._loop
        queue = self.queues[channel]

        def sink(_channel: str, pcm: bytes, rms: float) -> None:
            if loop is None or channel in self._injecting:
                return
            loop.call_soon_threadsafe(self._offer, queue, pcm, rms)

        return sink

    def _offer(self, queue: asyncio.Queue, pcm: bytes, rms: float) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            # 偶发丢帧正常（主线程忙时），持续丢帧说明识别跟不上，值得看一眼
            if self.dropped % 100 == 1:
                log.warning("音频队列已满，丢弃最旧帧（累计 %d 帧）", self.dropped)
        try:
            queue.put_nowait((pcm, rms))
        except asyncio.QueueFull:
            pass

    def _make_error_handler(self, channel: str):
        def handler(_channel: str, exc: Exception) -> None:
            log.error(
                "%s通道采集中断: %s",
                "面试官" if channel == "interviewer" else "麦克风",
                exc,
                exc_info=exc,
            )
            if self._loop is not None and self._broadcast is not None:
                message = f"{'面试官' if channel == 'interviewer' else '麦克风'}通道采集中断：{exc}"
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "error", "message": message}), self._loop
                )

        return handler

    # ---------- 通道消费 ----------
    async def _run_channel(self, channel: str) -> None:
        engine = self.engines[channel]
        queue = self.queues[channel]
        vad = EnergyVAD(settings.vad_threshold, settings.vad_silence_ms, settings.frame_ms)
        current = ""
        # 注入期间已经出现端点、但被压住没切；等注入结束立刻补一刀
        pending_final = False

        while True:
            # 面试中允许切换识别引擎：下一帧自动用新引擎，旧引擎的残句丢掉
            current_engine = self.engines.get(channel)
            if current_engine is not None and current_engine is not engine:
                engine = current_engine
                current = ""
                vad.reset()
                pending_final = False

            try:
                pcm, rms = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # 通道没声音（设备休眠、没人说话）：持续喂 30ms 静音。
                # 一是让 VAD 有稳定的静音节奏，二是别让云端 ASR 因为长时间没数据把连接掐掉。
                pcm, rms = b"\x00\x00" * 480, 0.0

            self.levels[channel] = rms
            speaking, cut = vad.push(rms)

            try:
                text, endpoint = await asyncio.to_thread(engine.push, pcm)
            except Exception:  # noqa: BLE001 - 单帧失败不能拖垮整条通道
                log.exception("识别失败 %s", channel)
                continue

            text = (text or "").strip()
            if text != current:
                current = text
                # 已定稿的这一句又被 ASR 回放时不要再广播 partial：
                # 否则界面会多出一个"实时"气泡，而后续重复定稿已被去重，永久清不掉。
                repeated = (
                    classify_repeat(
                        self._last_final.get(channel, ""), text, 0.0, settings.transcript_dedup_seconds
                    )
                    != "new"
                )
                if text and not repeated:
                    await self._broadcast({"type": "partial", "channel": channel, "text": text})
                    # 投机意图识别：拿半截转写先预判，定稿时直接复用，省掉 1s 左右
                    if channel == "interviewer" and self.pipeline is not None:
                        self.pipeline.feed_partial(text)

            # 注入音频期间（仿真念稿/回放）不切句：整段回答要落成一条转写，
            # 不能被 ASR 自带的端点检测从中间切开。端点信号先记着，注入结束再定稿。
            injecting = channel in self._injecting
            if endpoint or (cut and current):
                if injecting:
                    pending_final = True
                else:
                    log.debug(
                        "端点判定[%s]: endpoint=%s cut=%s 文本=%r", channel, endpoint, cut, current[:60]
                    )
                    await asyncio.to_thread(engine.reset)
                    if current:
                        await self.emit_final(channel, current)
                    current = ""
                    vad.reset()
                    pending_final = False
            elif pending_final and not injecting and current:
                log.debug("注入结束补定稿[%s]: 文本=%r", channel, current[:60])
                await asyncio.to_thread(engine.reset)
                await self.emit_final(channel, current)
                current = ""
                vad.reset()
                pending_final = False

    # ---------- 状态广播 ----------
    async def _status_loop(self) -> None:
        last_loud = time.time()
        last_warn = 0.0
        while True:
            await asyncio.sleep(0.25)
            if not self.active:
                last_loud = time.time()
                continue

            # 真实面试专用体检：环回 25 秒没声音，多半是会议软件输出到了别的扬声器。
            # 仿真面试的声音是注入进来的，不走这条路，所以不提示。
            now = time.time()
            if self.levels["interviewer"] >= settings.vad_threshold:
                last_loud = now
            elif (
                self.meta.get("mode") == "real"
                and now - last_loud > 25
                and now - last_warn > 120
            ):
                last_warn = now
                device = self.devices.get("interviewer") or "当前选中的扬声器"
                log.warning("面试官通道 25 秒没有声音（设备=%s）", device)
                await self._broadcast(
                    {
                        "type": "note",
                        "message": (
                            f"面试官通道 25 秒没有声音：确认会议软件的声音是从「{device}」放出来的，"
                            "或换个扬声器环回再试"
                        ),
                    }
                )

            try:
                await self._broadcast(
                    {
                        "type": "status",
                        "levels": self.levels,
                        "devices": self.devices,
                        "engine": self.engine_kind,
                        "dropped": self.dropped,
                        "interviewer_speaking": self.levels["interviewer"] >= settings.vad_threshold,
                        "candidate_speaking": self.levels["candidate"] >= settings.vad_threshold,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

    async def abort_start(self) -> None:
        """start() 中途失败（设备打不开、引擎建不起来）时清理半成品，别让采集线程/引擎泄漏。"""
        for capture in list(self.captures.values()):
            try:
                await asyncio.to_thread(capture.stop)
            except Exception:  # noqa: BLE001
                pass
        self.captures.clear()
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()
        for engine in list(self.engines.values()):
            try:
                await asyncio.to_thread(engine.close)
            except Exception:  # noqa: BLE001
                pass
        self.engines.clear()
        self.queues = {}
        self.active = False
        llmlog.set_interview("")


session = Session()
