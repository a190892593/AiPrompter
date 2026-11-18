"""仿真面试：把题库里的问题用 TTS 播给系统听，再把生成的话术播回麦克风通道。

和 tools/simulate.py 是同一套思路，只是搬到服务端，页面点一下就能跑。

健壮性约定：任何一句话的 TTS 失败都不允许中断整场仿真——
语音合成不出来就退化成"只写入文字"，让对话流和意图识别流程继续推进。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import random
import re
from typing import Callable

from . import judge, tts

log = logging.getLogger("simulator")

# 简历追问的“切入角度”：随机挑一个，避免每次开场都问简历里同一个指标
RESUME_ANGLES = [
    "项目背景与目标：当时要解决什么问题、成功标准是什么",
    "技术选型与取舍：为什么选它、对比过什么、放弃的方案差在哪",
    "具体实现与瓶颈：链路怎么搭的、最难的一段在哪",
    "效果数据与评估口径：指标怎么定义、在哪测的、基线是多少",
    "线上故障与复盘：出过什么事故、怎么定位、后来怎么防",
    "性能、扩展与成本：量级涨十倍会怎样、哪里先崩、成本怎么控",
    "团队协作与推进：方案怎么落地、和谁配合、遇到什么阻力",
    "如果重做一次：哪些会改、哪些坑不会再踩",
]


def _resume_hints(cfg) -> list[str]:
    """从简历里挑出可问的“经历行”，供随机选题用。"""
    text = (getattr(cfg, "resume", "") or "").strip()
    if not text:
        return []
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"^[\s#>*\-•·\d.、（）()]+", "", raw).strip()
        if len(line) >= 12:
            lines.append(line)
    keywords = (
        "项目", "系统", "平台", "负责", "优化", "提升", "落地", "架构", "召回",
        "检索", "数据", "指标", "团队", "交付", "开发", "搭建", "设计",
    )
    preferred = [line for line in lines if any(word in line for word in keywords)]
    return preferred or lines or [text[:200]]


class Simulator:
    def __init__(
        self,
        session,
        questions: list[str],
        echo: bool = True,
        gap: float = 1.5,
        audible: bool = False,
        more_questions: Callable[[], list[str]] | None = None,
        mode: str = "bank",
        max_questions: int = 20,
    ) -> None:
        self.session = session
        self.questions = list(questions)
        self._queue = list(questions)
        self._asked: set[str] = set()
        # 题库问完一轮后用来继续抽题；为 None 表示问完就结束
        self._more_questions = more_questions
        # bank：题库随机抽题；resume：根据简历 + 岗位职责生成问题
        self.mode = mode if mode in ("bank", "resume") else "bank"
        self.echo = echo
        self.gap = gap
        self.audible = audible
        self.max_questions = max_questions
        self.task: asyncio.Task | None = None
        self.running = False
        self.index = 0

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        # 仿真不自己实现意图识别/答案生成：提问和候选人回答都喂给同一个 session.pipeline，
        # 与真实面试共用同一套 feed_final → judge → produce_answer 链路。
        log.info("仿真面试启动：复用会话 Pipeline（意图识别/答案生成与真实面试同一实现）")
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()

    # ---------- 内部 ----------
    async def _wait(self, predicate: Callable[[], bool], timeout: float) -> bool:
        started = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started < timeout:
            if predicate():
                return True
            await asyncio.sleep(0.2)
        return False

    async def _say(self, channel: str, text: str) -> bool:
        """合成并注入一段话。成功返回 True；TTS 不可用时返回 False（调用方降级处理）。"""
        try:
            wav = await asyncio.to_thread(tts.synthesize_with_fallback, text)
            pcm = await asyncio.to_thread(tts.wav_to_pcm16, wav)
        except Exception as exc:  # noqa: BLE001 - TTS 坏了也不能中断仿真
            log.warning("仿真语音合成失败（%s）: %s", channel, exc)
            return False

        try:
            if self.audible:
                # 播放时把两路物理采集都关掉：否则扬声器的声音会被环回/麦克风再收一遍，
                # 导致同一句话重复上屏、甚至串到另一个通道去。
                device = ""
                if isinstance(self.session.devices, dict):
                    device = str(self.session.devices.get("interviewer") or "")
                async with self.session.suppress_capture("interviewer", "candidate"):
                    play = asyncio.create_task(asyncio.to_thread(tts.play_with_device, wav, device))
                    await self.session.feed_audio(channel, pcm)
                    await play
            else:
                await self.session.feed_audio(channel, pcm)
        except Exception as exc:  # noqa: BLE001
            log.warning("仿真音频注入失败（%s）: %s", channel, exc)
            return False
        return True

    def _similar_question(self, question: str) -> bool:
        """和已经问过的题太像（只换了说法）也算重复。"""
        for previous in self._asked:
            if difflib.SequenceMatcher(None, question, previous).ratio() >= 0.78:
                return True
        return False

    async def _resume_question(self) -> str:
        """按简历 + 岗位职责生成下一问；失败时自动切回题库。"""
        cfg = getattr(self.session, "config", None)
        if cfg is None or not ((cfg.resume or "").strip() or (cfg.background or "").strip()):
            await self.session.broadcast_note("没有简历/岗位职责，继续用题库提问")
            self.mode = "bank"
            self.session.meta["sim_mode"] = "bank"
            return ""

        pipeline = self.session.pipeline
        history = pipeline.history if pipeline is not None else []
        hints = _resume_hints(cfg)
        last_question = ""
        for _ in range(6):
            angle = random.choice(RESUME_ANGLES)
            if hints:
                # 不聊数据/指标时，尽量别再拿简历里同一个 KPI 开场
                if any(word in angle for word in ("数据", "指标", "效果")):
                    hint = random.choice(hints)
                else:
                    non_metric = [
                        item
                        for item in hints
                        if not re.search(r"(提升|下降|从\s*\d|%|万|亿|\d+\s*个点)", item)
                    ]
                    hint = random.choice(non_metric or hints)
            else:
                hint = ""
            try:
                question = await judge.generate_question(
                    cfg,
                    history,
                    sorted(self._asked),
                    trace="sim-question",
                    angle=angle,
                    hint=hint,
                )
            except Exception:  # noqa: BLE001 - 生成失败回退题库
                log.exception("简历提问生成失败")
                break
            question = (question or "").strip().splitlines()[0].strip() if question else ""
            if not question:
                continue
            last_question = question
            if question not in self._asked and not self._similar_question(question):
                self._asked.add(question)
                return question
        if last_question:
            # 连续几次都比较像：先问出去，避免仿真卡住；下一轮会换角度
            self._asked.add(last_question)
            return last_question
        await self.session.broadcast_note("简历提问生成失败，先用题库继续")
        self.mode = "bank"
        self.session.meta["sim_mode"] = "bank"
        return ""

    async def _next_question(self) -> str | None:
        """取下一道题；题库模式一轮问完后继续抽，简历模式实时生成。"""
        while self.running and self.session.active:
            if self.mode == "resume":
                question = await self._resume_question()
                if question:
                    return question
                # 生成失败会自动切回 bank，继续走下面的题库逻辑

            while self._queue:
                text = self._queue.pop(0).strip()
                if text and text not in self._asked:
                    self._asked.add(text)
                    return text
            if self._more_questions is None:
                return None
            try:
                fresh = await asyncio.to_thread(self._more_questions)
            except Exception:  # noqa: BLE001 - 补题失败就正常收尾
                log.exception("补充仿真题目失败")
                return None
            fresh = [text.strip() for text in (fresh or []) if text and text.strip()]
            remaining = [text for text in fresh if text not in self._asked]
            if remaining:
                self._queue = remaining
                continue
            # 题库里只有一道题时重复问没有意义，直接结束
            if len(set(fresh)) <= 1:
                return None
            # 已经完整过了一轮：清空已问记录，再随机来一轮
            self._asked.clear()
            self._queue = fresh
            await self.session.broadcast_note("题库已过一轮，继续随机出题")
        return None

    async def _run(self) -> None:
        pipeline = self.session.pipeline
        if pipeline is None:
            self.running = False
            return

        warned_tts = False
        index = 0
        hit_limit = False
        try:
            while self.running and self.session.active:
                if self.max_questions > 0 and index >= self.max_questions:
                    hit_limit = True
                    break
                question = await self._next_question()
                if not question:
                    break
                index += 1
                self.index = index
                log.info("仿真第 %s 问：%s", index, question[:30])

                before = len(pipeline.qa)
                spoken = await self._say("interviewer", question)
                if not spoken and not warned_tts:
                    warned_tts = True
                    await self.session.broadcast_note("语音合成不可用，仿真改为纯文字模式")
                if not spoken:
                    # 合成不了就只写文字，意图识别流程照常走（emit_final 会广播给界面）
                    await self.session.emit_final("interviewer", question)

                got = await self._wait(lambda: len(pipeline.qa) > before, 40)
                if not got:
                    log.warning("第 %s 问没判出问题，跳过", index)
                    await asyncio.sleep(1)
                    continue

                entry = pipeline.qa[-1]
                await self._wait(lambda: bool(entry.get("answer")), 60)

                answer = str(entry.get("answer") or "").strip()
                if self.echo and answer:
                    # 完整念出来；只在极端长答案时截断，避免仿真一场跑太久
                    spoken_answer = answer if len(answer) <= 600 else answer[:600]
                    spoke_answer = await self._say("candidate", spoken_answer)
                    if not spoke_answer:
                        # 候选人这句话合成失败：直接把文字写进对话流并挂到当前问题上
                        await self.session.emit_final("candidate", spoken_answer)
                    await asyncio.sleep(self.gap)

            if self.running and self.session.active:
                if hit_limit:
                    await self.session.broadcast_note(
                        f"本场仿真已问满 {self.max_questions} 题，可以点『结束并评分』"
                    )
                else:
                    await self.session.broadcast_note("题库里的题问完了，可以点『结束并评分』")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 仿真失败不能拖垮服务
            log.exception("仿真中断")
        finally:
            self.running = False
