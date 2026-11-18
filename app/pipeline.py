"""意图识别流水线：转写定稿 → 意图识别 → 归纳 → 流式参考答案 → 评分。

音频来源已经不关心了（现在是本地桥直接喂），这里只处理文字与事件。
"""

from __future__ import annotations

import asyncio
import json
import difflib
import logging
import re
import time
import uuid
from typing import Awaitable, Callable

from . import bank, judge, memory, profile, prompts, report
from .config import settings
from .llm import LLMError, llm
from .schemas import SessionConfig

log = logging.getLogger("pipeline")

# 半句里出现这些特征，才值得提前预判（避免对陈述句浪费模型调用）
_QUESTION_HINTS = (
    "什么", "怎么", "为什么", "如何", "多少", "哪", "是否",
    "能不能", "有没有", "区别", "确定吗", "真的吗", "凭什么",
    "有数据吗", "怎么证明", "吗", "呢",
)


def _looks_like_question_hint(text: str) -> bool:
    return any(hint in text for hint in _QUESTION_HINTS)

Broadcast = Callable[[dict], Awaitable[None]]

# 回答里不该出现的占位符（XX、【…】、$ 这种），出现就让模型改写一次
_PLACEHOLDER_HINT = re.compile(r"[【\[]|(?:XX|xx|X 万|N 万|若干|待填)")


def _has_placeholders(text: str) -> bool:
    return bool(text) and bool(_PLACEHOLDER_HINT.search(text))


def _merge_refs(lists: list[list[dict]], limit: int = 3) -> list[dict]:
    """多路检索结果按「共识分」合并去重：被越多路命中、名次越靠前越优先。"""
    scores: dict[int, float] = {}
    first_rank: dict[int, int] = {}
    items_by_id: dict[int, dict] = {}
    for lane in lists:
        for rank, item in enumerate(lane):
            item_id = item.get("id")
            if item_id is None:
                continue
            items_by_id.setdefault(item_id, item)
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank + 1)
            first_rank[item_id] = min(first_rank.get(item_id, rank), rank)
    ranked = sorted(scores, key=lambda item_id: (-scores[item_id], first_rank[item_id]))
    return [items_by_id[item_id] for item_id in ranked[:limit]]


class Pipeline:
    def __init__(self, cfg: SessionConfig, broadcast: Broadcast) -> None:
        self.cfg = cfg
        self._broadcast = broadcast

        self.history: list[dict] = []
        self.qa: list[dict] = []
        self._current: dict | None = None
        self._judge_task: asyncio.Task | None = None
        self._answer_task: asyncio.Task | None = None
        self._verify_task: asyncio.Task | None = None
        self._rewrite_task: asyncio.Task | None = None
        self._recent: list[tuple[float, str]] = []
        self.degraded = False
        self._report: dict | None = None
        # 意图识别里顺带纠正的整句转写，回写气泡用
        self._transcript_hook = None
        # 参考检索预热（与判题并行）
        self._prefetch: tuple[float, list[dict], str] | None = None
        self._prefetch_task: asyncio.Task | None = None
        # 同一句话重复出现（ASR 抖动/面试官重复）时的判题缓存
        self._verdict_cache: dict[str, tuple[float, dict]] = {}
        # 每一次意图识别的结果流水（含被跳过的），随会话落库，刷新/回放都能看
        self.judge_log: list[dict] = []
        # 意图识别实时统计（提词器顶部展示，便于判断质量）
        self.stats: dict[str, int] = {
            "judge_total": 0,
            "accepted": 0,
            "skipped": 0,
            "skip_aside": 0,
            "skip_statement": 0,
            "skip_low_conf": 0,
            "skip_duplicate": 0,
            "skip_verify": 0,
            "verify_runs": 0,
            "verify_rejected": 0,
            "verify_fixed": 0,
            "followups": 0,
            "challenges": 0,
            "max_depth": 0,
            "judge_ms_total": 0,
            "judge_ms_max": 0,
            "corrections": 0,
        }
        # 投机意图识别：半截转写先预判，定稿时命中就复用
        self._prejudge: dict | None = None
        self._prejudge_task: asyncio.Task | None = None

    # ---------- 外部输入 ----------
    async def feed_final(self, channel: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        log.debug("流水线收到定稿[%s]: %r（历史 %d 句）", channel, text[:80], len(self.history) + 1)
        self.history.append({"channel": channel, "text": text, "ts": time.time()})
        if len(self.history) > 400:
            self.history = self.history[-200:]

        if channel == "candidate":
            if self._current is not None and not self._current.get("closed"):
                self._current["user_answer"] = (
                    self._current.get("user_answer", "") + " " + text
                ).strip()
                log.debug(
                    "候选人回答已挂到卡片 %s（累计 %d 字）",
                    self._current.get("id"),
                    len(self._current["user_answer"]),
                )
            return

        self._schedule_judge()

    # ---------- 投机意图识别（用半截转写提前判） ----------
    _PREJUDGE_MIN_LEN = 6

    def feed_partial(self, text: str) -> None:
        """ASR 还在吐字时先预判一次，定稿时命中就省掉整次模型调用。"""
        candidate = (text or "").strip()
        if not settings.speculative_intent:
            return
        if len(candidate) < self._PREJUDGE_MIN_LEN:
            return
        # 太碎、没有提问特征的半句不值得预判
        if not _looks_like_question_hint(candidate):
            return
        if self._prejudge_task and not self._prejudge_task.done():
            self._prejudge_task.cancel()
        self._prejudge_task = asyncio.create_task(self._prejudge_after(candidate))

    async def _prejudge_after(self, partial: str) -> None:
        try:
            await asyncio.sleep(0.45)  # 等半句再稳定一点
            if not self.history and not partial:
                return
            history = list(self.history)
            history.append({"channel": "interviewer", "text": partial, "ts": time.time()})
            verdict = await judge.classify(self.cfg, history, examples=memory.similar(partial, 3))
            if verdict.get("type") in ("question", "instruction") and verdict.get("question"):
                self._prejudge = {
                    "text": partial,
                    "verdict": verdict,
                    "ts": time.monotonic(),
                }
                log.info("投机预判完成: %r", partial[:40])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 预判失败不影响主链路
            log.debug("投机预判失败", exc_info=True)

    _PREJUDGE_REVISE_MARKERS = ("不是", "我是说", "说错了", "算了", "重新问", "不对", "不问了", "换个问题", "先不聊")

    def _take_prejudge(self, final_text: str) -> dict | None:
        item = self._prejudge
        self._prejudge = None
        if not item:
            return None
        if not settings.speculative_intent:
            return None
        if time.monotonic() - float(item["ts"]) > 12:
            return None
        if any(marker in final_text for marker in self._PREJUDGE_REVISE_MARKERS):
            log.info("投机预判放弃：定稿出现自纠/取消措辞: %r", final_text[:40])
            return None
        ratio = difflib.SequenceMatcher(None, item["text"], final_text).ratio()
        if ratio < 0.9:
            log.info("投机预判放弃：相似度不足 %.2f", ratio)
            return None
        if len(final_text) > len(item["text"]) * 1.8 or len(final_text) < len(item["text"]) * 0.5:
            log.info("投机预判放弃：定稿与半句长度差异过大")
            return None
        log.info("投机预判命中: 相似度=%.2f 省一次意图识别调用", ratio)
        return item["verdict"]

    async def close(self) -> None:
        for task in (self._judge_task, self._answer_task, self._verify_task, self._rewrite_task):
            if task and not task.done():
                task.cancel()

    async def finish(self) -> dict:
        if self._report is None:
            started = time.perf_counter()
            log.info("开始评分: 卡片=%d 张 历史=%d 句", len(self.qa), len(self.history))
            self._report = await report.build_report(self.cfg, self.qa)
            log.info(
                "评分完成: 总分=%s 用时=%.1fs",
                self._report.get("overall_score"),
                time.perf_counter() - started,
            )
        # 话题链统计：几条链、最深几层、追问/反问各多少次，复盘和评分用
        if isinstance(self._report, dict) and "chain_stats" not in self._report:
            chains: dict[str, list[dict]] = {}
            for item in self.qa:
                chains.setdefault(str(item.get("root_id") or item.get("id")), []).append(item)
            self._report["chain_stats"] = {
                "chains": len(chains),
                "max_depth": max((len(v) for v in chains.values()), default=0),
                "followups": sum(1 for item in self.qa if item.get("followup")),
                "challenges": sum(1 for item in self.qa if item.get("challenge")),
                "chain_rounds": sum(len(v) for v in chains.values() if len(v) > 1),
            }
        return self._report

    # ---------- 意图识别 ----------
    _SENTENCE_END = tuple("。！？!?…；;.\n")
    _TAIL_FILLERS = ("那个", "这个", "我是说", "如果", "就是说", "然后", "而且", "因为", "就是说")

    def _adaptive_debounce(self) -> float:
        """句子已经说完就只等 120ms；明显没说完才等 600ms。"""
        last = next(
            (item.get("text", "") for item in reversed(self.history) if item.get("channel") == "interviewer"),
            "",
        ).strip()
        if not last:
            return 0.5
        ended = last.endswith(self._SENTENCE_END) or last.rstrip().endswith(("？", "?"))
        complete = ended or (len(last) >= 12 and not last.endswith(self._TAIL_FILLERS))
        return 0.12 if complete else 0.6

    def _start_prefetch(self, query: str) -> None:
        """判题期间并行预热参考题库检索/记忆，话术生成时直接取。"""
        if not query.strip():
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return

        async def _run() -> None:
            try:
                refs, mem = await asyncio.gather(
                    asyncio.to_thread(bank.search, query, 2),
                    asyncio.to_thread(profile.memory_text, 3),
                )
                self._prefetch = (time.monotonic(), refs, mem)
            except Exception:  # noqa: BLE001 - 预热失败不影响主流程
                log.debug("参考检索预热失败", exc_info=True)

        self._prefetch_task = asyncio.create_task(_run())

    # 意图识别
    def stats_view(self) -> dict:
        data = dict(self.stats)
        total = data.get("judge_total") or 0
        data["avg_ms"] = round(data.get("judge_ms_total", 0) / total) if total else 0
        data["skip_total"] = data.get("skipped", 0)
        return data

    async def _broadcast_stats(self) -> None:
        if self._broadcast is not None:
            try:
                await self._broadcast(
                    {"type": "intent_stats", "stats": self.stats_view(), "ts": time.time()}
                )
            except Exception:  # noqa: BLE001 - 统计广播失败不影响面试
                log.debug("统计广播失败", exc_info=True)

    def _schedule_judge(self) -> None:
        if self._judge_task and not self._judge_task.done():
            log.debug("新的面试官转写到达，取消上一次未完成的意图识别")
            self._judge_task.cancel()
        self._judge_task = asyncio.create_task(self._judge_after_debounce())

    async def _judge_after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._adaptive_debounce())
            await self._judge()
        except asyncio.CancelledError:
            log.debug("意图识别被新的转写打断（正常现象，会重新判）")
            raise
        except Exception:  # noqa: BLE001
            # 这条很重要：意图识别任务异常退出时，界面会一直停在「意图识别中」，这里必须留痕
            log.exception("意图识别流程异常退出（界面会卡在「意图识别中」，请把这段日志发出来）")

    async def _judge(self) -> None:
        last = next(
            (item["text"] for item in reversed(self.history) if item["channel"] == "interviewer"), ""
        )
        if not last:
            return

        # 意图识别过程对用户可见：先广播"开始判"，判完再广播结论（无论是否出卡片）
        judge_trace = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        log.info(
            "意图识别开始 trace=%s 原话=%r 上下文=%d句 去抖=%sms 超时=%.0fs",
            judge_trace,
            last[:100],
            min(len(self.history), 20),
            settings.judge_debounce_ms,
            settings.judge_timeout,
        )
        await self._broadcast(
            {"type": "judge_start", "raw": last, "trace": judge_trace, "ts": time.time()}
        )
        self._start_prefetch(last)

        timed_out = False
        verdict: dict | None = None
        cached_key = re.sub(r"[\s，。！？、,.!?；;：:]+", "", last)
        cached = self._verdict_cache.get(cached_key)
        if cached is not None and time.monotonic() - cached[0] < 45:
            log.info("意图识别命中缓存（同一句话 45s 内不再调模型）")
            verdict = cached[1]
            self._take_prejudge(last)  # 清掉可能存在的预判
        else:
            verdict = self._take_prejudge(last)
        if verdict is not None:
            log.info("意图识别走预判/缓存结果（省一次模型调用）")
        else:
            try:
                verdict = await asyncio.wait_for(
                    judge.classify(
                        self.cfg,
                        self.history,
                        examples=memory.similar(last, 3),
                        trace=judge_trace,
                        verify=False,
                        refine=False,
                    ),
                    timeout=settings.judge_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                log.warning("意图识别超时（>%.0fs），改用规则兜底", settings.judge_timeout)
                verdict = judge.heuristic_classify(self.history)
        corrected_text = str(verdict.get("corrected") or "").strip()
        if corrected_text and corrected_text != last:
            # 更新流水线上下文（后续判题看到的是纠正后的句子）
            for item in reversed(self.history):
                if item.get("channel") == "interviewer":
                    item["text"] = corrected_text
                    break
            hook = getattr(self, "_transcript_hook", None)
            if callable(hook):
                try:
                    hook("interviewer", last, corrected_text)
                except Exception:  # noqa: BLE001 - 回写失败不影响判题
                    log.exception("转写纠正回写失败")
            await self._broadcast(
                {
                    "type": "final_update",
                    "channel": "interviewer",
                    "text": corrected_text,
                    "ts": time.time(),
                }
            )
            log.info("意图识别顺带纠正转写: %r -> %r", last[:40], corrected_text[:40])

        duration_ms = int((time.perf_counter() - started) * 1000)
        if not timed_out:
            self._verdict_cache[cached_key] = (time.monotonic(), dict(verdict))
            if len(self._verdict_cache) > 60:
                self._verdict_cache = dict(list(self._verdict_cache.items())[-40:])
        self.stats["judge_total"] = self.stats.get("judge_total", 0) + 1
        self.stats["judge_ms_total"] = self.stats.get("judge_ms_total", 0) + duration_ms
        self.stats["judge_ms_max"] = max(self.stats.get("judge_ms_max", 0), duration_ms)
        kind = verdict["type"]
        confidence = verdict["confidence"]
        question = verdict["question"]
        focus = verdict["focus"]

        async def _report(accepted: bool, skip_reason: str = "") -> None:
            log.info(
                "意图识别结论 trace=%s 类型=%s 置信度=%.2f 用时=%dms 超时兜底=%s %s",
                judge_trace,
                kind,
                float(confidence),
                duration_ms,
                timed_out,
                f"跳过（{skip_reason}）" if not accepted else f"出卡问题={question!r}",
            )
            entry = {
                "type": "judge_result",
                "raw": last,
                "kind": kind,
                "confidence": round(float(confidence), 2),
                "question": question,
                "focus": focus,
                "spans": verdict.get("spans", 1),
                "reason": verdict.get("reason", ""),
                "accepted": accepted,
                "skip_reason": skip_reason,
                "verify": verdict.get("verify"),
                "timed_out": timed_out,
                "trace": judge_trace,
                "duration_ms": duration_ms,
                "ts": time.time(),
            }
            # 存一份精简版到会话记录里（提词器/详情页的「意图识别记录」）
            self.judge_log.append(
                {
                    key: entry[key]
                    for key in (
                        "raw",
                        "kind",
                        "confidence",
                        "question",
                        "reason",
                        "accepted",
                        "skip_reason",
                        "timed_out",
                        "trace",
                        "duration_ms",
                        "ts",
                    )
                }
            )
            if len(self.judge_log) > 300:
                del self.judge_log[:-300]
            await self._broadcast(entry)
            if accepted:
                self.stats["accepted"] = self.stats.get("accepted", 0) + 1
            else:
                self.stats["skipped"] = self.stats.get("skipped", 0) + 1
                if "旁白" in skip_reason:
                    self.stats["skip_aside"] = self.stats.get("skip_aside", 0) + 1
                elif "重复" in skip_reason:
                    self.stats["skip_duplicate"] = self.stats.get("skip_duplicate", 0) + 1
                elif "二次校验" in skip_reason:
                    self.stats["skip_verify"] = self.stats.get("skip_verify", 0) + 1
                elif "置信度" in skip_reason:
                    self.stats["skip_low_conf"] = self.stats.get("skip_low_conf", 0) + 1
                else:
                    self.stats["skip_statement"] = self.stats.get("skip_statement", 0) + 1
            await self._broadcast_stats()

        if kind == "aside":
            await _report(False, "旁白/离场，不是提问")
            await self._broadcast({"type": "aside", "text": last, "reason": verdict.get("reason", "")})
            return
        if kind not in ("question", "instruction"):
            await _report(False, f"判定为 {kind}，不需要回答")
            return
        if confidence < settings.judge_confidence:
            await _report(False, f"置信度 {confidence:.0%} 低于门槛")
            return
        verify = verdict.get("verify") or {}
        if verify.get("ok") is False:
            await _report(False, f"二次校验未通过：{verify.get('reason') or '问题不成立'}")
            return
        search_queries = [item for item in (verdict.get("search_queries") or []) if str(item).strip()]
        needs_rewrite = bool(not question or judge._looks_generic(question))
        if needs_rewrite:
            fallback = judge._fallback_question(self.history)
            question = fallback or last[:60]
        followup = bool(verdict.get("followup"))
        challenge = bool(verdict.get("challenge"))
        if self._is_duplicate(question, followup=followup):
            await _report(False, "与最近的问题重复")
            return
        await _report(True)

        self._recent.append((time.monotonic(), question))
        question_id = uuid.uuid4().hex[:8]
        log.info(
            "出卡 id=%s 问题=%r 追问=%s 聚焦=%s 原话=%r",
            question_id,
            question,
            followup,
            focus,
            last[:80],
        )

        prev_entry = self._current or (self.qa[-1] if self.qa else None)
        # ---- 追问/反问链：parent/root/depth + 整条链的上文 ----
        is_chain = followup or challenge
        parent = prev_entry if is_chain else None
        parent_id = parent.get("id") if parent else None
        root_id = (parent.get("root_id") or parent.get("id")) if parent else question_id
        depth = (int(parent.get("depth") or 1) + 1) if parent else 1
        chain_text = ""
        if parent:
            members = [
                item
                for item in self.qa
                if (item.get("root_id") or item.get("id")) == root_id
            ]
            lines = []
            for index, item in enumerate(members, start=1):
                tag = ""
                if item.get("challenge"):
                    tag = "（反问）"
                elif item.get("followup"):
                    tag = "（追问）"
                lines.append(f"第{index}问{tag}：{item.get('question', '')}")
                answer_hint = (item.get("answer") or "").strip()
                if answer_hint:
                    lines.append(f"  已答要点：{answer_hint[:160]}")
            chain_text = "\n".join(lines)
        if challenge and followup:
            mode_note = (
                "面试官在追问上一问，同时带有质疑——先承接上一问的结论/数字，"
                "再正面回应质疑（给数据、线上结果或边界取舍），最后补细节"
            )
        elif challenge:
            mode_note = (
                "面试官在质疑/验证你的回答——先正面回应质疑（给数据、线上结果或边界取舍），"
                "不要只喊“没问题/肯定可以”，再展开细节"
            )
        elif followup:
            mode_note = "面试官在追问上一问——先承接上一问的结论/数字，再回答新增部分，不要重复整段"
        else:
            mode_note = "正常提问，直接回答"
        prev_qa_text = chain_text
        if not prev_qa_text and followup and prev_entry:
            prev_qa_text = (
                f"上一问：{prev_entry.get('question', '')}\n"
                f"上一版话术：{(prev_entry.get('answer') or '')[:220]}"
            ).strip()

        if self._current is not None and not self._current.get("closed"):
            self._current["closed"] = True

        self._current = {
            "id": question_id,
            "question": question,
            "focus": focus,
            "answer": "",
            "user_answer": "",
            "level": self.cfg.answer_level,
            "ts": time.time(),
            "closed": False,
            "followup": followup,
            "challenge": challenge,
            "prev_id": parent_id,
            "parent_id": parent_id,
            "root_id": root_id,
            "depth": depth,
            "rejected": False,
            "verify_reason": "",
            "rewriting": needs_rewrite,
            "search_queries": search_queries,
        }
        self.qa.append(self._current)
        if followup:
            self.stats["followups"] = self.stats.get("followups", 0) + 1
        if challenge:
            self.stats["challenges"] = self.stats.get("challenges", 0) + 1
        self.stats["max_depth"] = max(self.stats.get("max_depth", 0), depth)
        await self._broadcast_stats()

        await self._broadcast(
            {
                "type": "question",
                "id": question_id,
                "summary": question,
                "focus": focus,
                "confidence": round(confidence, 2),
                "spans": verdict["spans"],
                "reason": verdict.get("reason", ""),
                "kind": kind,
                "raw": last,
                "followup": followup,
                "challenge": challenge,
                "rewriting": needs_rewrite,
                "prev_id": parent_id,
                "parent_id": parent_id,
                "root_id": root_id,
                "depth": depth,
                "parent_question": (parent or {}).get("question", ""),
                "degraded": self.degraded,
            }
        )

        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = asyncio.create_task(
            self._produce_answer(
                question_id,
                question,
                focus,
                prev_qa=prev_qa_text,
                mode_note=mode_note,
                search_queries=search_queries,
            )
        )
        if needs_rewrite:
            if self._rewrite_task and not self._rewrite_task.done():
                self._rewrite_task.cancel()
            self._rewrite_task = asyncio.create_task(
                self._post_rewrite(question_id, list(self.history), judge_trace)
            )

        # 卡片先出，复核异步跑；不通过就撤卡（不影响正常问题的出卡速度）
        needs_verify = (
            confidence < settings.intent_verify_below
            or int(verdict.get("spans") or 1) >= 2
            or followup
            or challenge
        )
        if settings.intent_verify and needs_verify:
            if self._verify_task and not self._verify_task.done():
                self._verify_task.cancel()
            self._verify_task = asyncio.create_task(
                self._post_verify(question_id, question, list(self.history), judge_trace)
            )

    async def _post_rewrite(
        self, question_id: str, history_snapshot: list[dict], trace: str
    ) -> None:
        """异步问题重写（二次纠偏）：第一次归纳为空/空泛时先出卡，重写完成再更新问题并重生成话术。"""
        try:
            refined = await judge._refine_question(self.cfg, history_snapshot, trace)
            card = next((item for item in self.qa if item.get("id") == question_id), None)
            if card is None:
                return
            card["rewriting"] = False
            if not refined or judge._looks_generic(refined):
                log.info("问题重写未得到更好的结果，保留兜底问题 id=%s", question_id)
                await self._broadcast(
                    {"type": "question_updated", "id": question_id, "summary": card["question"], "reason": "问题重写未改进"}
                )
                return
            old = card["question"]
            card["question"] = refined
            await self._broadcast(
                {
                    "type": "question_updated",
                    "id": question_id,
                    "summary": refined,
                    "reason": "问题重写（二次纠偏）",
                }
            )
            log.info("问题重写完成 id=%s: %r -> %r", question_id, old[:40], refined[:40])
            if (
                self._answer_task
                and not self._answer_task.done()
                and self._current
                and self._current.get("id") == question_id
            ):
                self._answer_task.cancel()
                self._answer_task = asyncio.create_task(
                    self._produce_answer(
                        question_id,
                        refined,
                        list(card.get("focus") or []),
                        search_queries=list(card.get("search_queries") or []),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 重写失败不影响已出的卡
            log.exception("问题重写异常")

    async def _post_verify(
        self, question_id: str, question: str, history_snapshot: list[dict], trace: str
    ) -> None:
        """异步复核：通过就什么都不做；不通过撤卡；措辞有偏差就修正问题并重新生成话术。"""
        try:
            check = await judge._verify(self.cfg, history_snapshot, question, trace)
            if check is None:
                return
            card = next((item for item in self.qa if item.get("id") == question_id), None)
            if card is None:
                return
            ok = check.get("ok", True)
            reason = str(check.get("reason") or "").strip()
            self.stats["verify_runs"] = self.stats.get("verify_runs", 0) + 1
            await self._broadcast(
                {"type": "verify_result", "id": question_id, "ok": ok, "reason": reason}
            )
            if not ok:
                self.stats["verify_rejected"] = self.stats.get("verify_rejected", 0) + 1
                card["rejected"] = True
                card["verify_reason"] = reason or "复核未通过"
                card["closed"] = True
                if (
                    self._answer_task
                    and not self._answer_task.done()
                    and self._current
                    and self._current.get("id") == question_id
                ):
                    self._answer_task.cancel()
                await self._broadcast(
                    {"type": "question_rejected", "id": question_id, "reason": card["verify_reason"]}
                )
                log.info("异步复核未通过，已撤卡 id=%s 原因=%s", question_id, card["verify_reason"])
                await self._broadcast_stats()
                return
            corrected = judge._clean_question(check.get("question") or "")
            if corrected and not judge._looks_generic(corrected) and corrected != card["question"]:
                old = card["question"]
                card["question"] = corrected
                self.stats["verify_fixed"] = self.stats.get("verify_fixed", 0) + 1
                await self._broadcast(
                    {
                        "type": "question_updated",
                        "id": question_id,
                        "summary": corrected,
                        "reason": reason or "复核修正",
                    }
                )
                log.info("异步复核修正问题 id=%s: %r -> %r", question_id, old[:40], corrected[:40])
                if (
                    self._answer_task
                    and not self._answer_task.done()
                    and self._current
                    and self._current.get("id") == question_id
                ):
                    self._answer_task.cancel()
                    self._answer_task = asyncio.create_task(
                        self._produce_answer(
                            question_id,
                            corrected,
                            list(card.get("focus") or []),
                            search_queries=list(card.get("search_queries") or []),
                        )
                    )
                await self._broadcast_stats()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 复核异常不影响面试
            log.exception("异步复核异常")

    # ---------- 人工干预 ----------
    async def suggest_questions(self, raw: str, current: str = "") -> list[str]:
        """候选人觉得答案不对时，让模型猜 5 个面试官可能真正想问的问题。"""
        if not llm.live:
            return []
        try:
            data = await llm.chat_json(
                [
                    {"role": "system", "content": prompts.render("suggest.system")},
                    {"role": "user", "content": prompts.render("suggest.user", raw=raw, current=current or raw)},
                ],
                temperature=0.8,
                max_tokens=400,
                category="候选问题",
            )
        except LLMError:
            log.warning("猜测真实问题失败（模型返回异常），返回空列表", exc_info=True)
            return []
        items = [str(x).strip() for x in (data.get("questions") or []) if str(x).strip()]
        return items[:5]

    async def ask_manually(
        self,
        question: str,
        raw: str = "",
        thinking: bool | None = None,
        web_search: bool = False,
    ) -> dict:
        """人工指定/修改问题，按新问题重新生成一张卡片。"""
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")

        question_id = uuid.uuid4().hex[:8]
        log.info(
            "人工指定问题: id=%s 问题=%r 思考=%s 联网=%s",
            question_id,
            question[:80],
            thinking,
            web_search,
        )
        if self._current is not None:
            self._current["closed"] = True
        entry = {
            "id": question_id,
            "question": question,
            "focus": [],
            "answer": "",
            "user_answer": "",
            "level": self.cfg.answer_level,
            "ts": time.time(),
            "closed": False,
        }
        self.qa.append(entry)
        self._current = entry

        await self._broadcast(
            {
                "type": "question",
                "id": question_id,
                "summary": question,
                "focus": [],
                "confidence": 1.0,
                "spans": 1,
                "reason": "人工指定",
                "kind": "question",
                "raw": raw or question,
                "degraded": self.degraded,
            }
        )
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = asyncio.create_task(
            self._produce_answer(
                question_id, question, [], thinking_override=thinking, use_web=web_search
            )
        )
        return {"id": question_id, "question": question}

    def _is_duplicate(self, question: str, followup: bool = False) -> bool:
        """追问经常和上一问共用词，重复判定要更严：只有几乎原样重复才拦截。"""
        now = time.monotonic()
        self._recent = [(stamp, text) for stamp, text in self._recent if now - stamp < 90]
        threshold = 0.96 if followup else 0.85
        return any(
            difflib.SequenceMatcher(None, previous, question).ratio() > threshold
            for _, previous in self._recent
        )

    # ---------- 话术 ----------
    async def _produce_answer(
        self,
        question_id: str,
        question: str,
        focus: list[str],
        thinking_override: bool | None = None,
        use_web: bool | None = None,
        prev_qa: str = "",
        mode_note: str = "",
        search_queries: list[str] | None = None,
    ) -> None:
        level = self.cfg.answer_level
        if use_web is None:
            # 自动答案：标准档/深度档自动联网（极简档不联网）
            web = bool(settings.answer_web_search) and level in ("standard", "deep")
        else:
            web = bool(use_web)
        if thinking_override is None:
            # 自动答案：只有深度档才开思考（极简/标准不走 reasoning，首字更快）
            thinking = bool(settings.llm_thinking) and level == "deep"
        else:
            thinking = bool(thinking_override)
        model = settings.answer_model
        answer_started = time.perf_counter()
        await self._broadcast(
            {"type": "answer_start", "id": question_id, "level": level, "thinking": thinking}
        )

        references: list[dict] = []
        memory_hint = ""
        prefetched = self._prefetch
        if prefetched is not None and time.monotonic() - prefetched[0] < 20:
            references, memory_hint = prefetched[1], prefetched[2]
            log.debug("话术使用预热的检索结果（与判题并行完成）")
        else:
            try:
                references, memory_hint = await asyncio.gather(
                    asyncio.to_thread(bank.search, question, 3),
                    asyncio.to_thread(profile.memory_text, 3),
                )
            except Exception:  # noqa: BLE001 - 检索失败不该让话术出不来
                log.warning("检索/记忆加载失败", exc_info=True)

        extra_queries = [
            str(item).strip()
            for item in (search_queries or [])
            if str(item).strip() and str(item).strip() != question
        ]
        if extra_queries:
            try:
                extra_lists = await asyncio.gather(
                    *[asyncio.to_thread(bank.search, item, 2) for item in extra_queries]
                )
                references = _merge_refs([references, *extra_lists], limit=3)
                log.info("检索改写多路召回: %s", extra_queries)
            except Exception:  # noqa: BLE001 - 多路失败退回单路
                log.warning("检索改写多路召回失败", exc_info=True)
        log.info(
            "开始生成参考答案: id=%s 问题=%r 档位=%s 模型=%s 思考=%s 联网=%s 参考=%d条 记忆=%d字",
            question_id,
            question,
            level,
            model,
            thinking,
            web,
            len(references),
            len(memory_hint),
        )

        # 三版各自实际使用的生成模式（界面列头展示）
        modes = {
            "standard": {"thinking": bool(thinking), "web": bool(web)},
            "brief": {"thinking": False, "web": False},
            "deep": {"thinking": bool(settings.llm_thinking), "web": bool(web)},
        }

        async def _polish(value: str) -> str:
            """占位符兜底改写（不再做英文注音）。"""
            if _has_placeholders(value):
                try:
                    fixed = await judge.fix_placeholders(value, question, trace=question_id)
                except LLMError as exc:
                    log.warning("占位符改写失败: %s", exc)
                    fixed = ""
                if fixed:
                    value = fixed
            return value.strip()

        async def _variant(lvl: str) -> str:
            # 模型偶尔返回空流，重试一次；还是空就不放进 variants
            for _ in range(2):
                try:
                    value = await judge.compose_answer(
                        self.cfg, question, focus, self.history, lvl, references, memory_hint,
                        trace=question_id, prev_qa=prev_qa, mode_note=mode_note,
                        thinking=bool(settings.llm_thinking) and lvl == "deep",
                        web_search=web and lvl == "deep",
                    )
                except LLMError as exc:
                    log.warning("%s 版生成失败: %s", lvl, exc)
                    value = ""
                if value.strip():
                    return await _polish(value)
            return ""

        # 变体和标准版同时开始，互不阻塞：三版总耗时 ≈ 最慢的那一版
        variant_task = asyncio.gather(_variant("brief"), _variant("deep"))

        text = ""
        reasoning = ""
        streamed = False
        messages = judge.answer_messages(
            self.cfg, question, focus, self.history, level, references, memory_hint,
            prev_qa=prev_qa, mode_note=mode_note,
        )
        if web:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "【联网搜索】回答之前必须先调用 deepseek_web_search 检索并核实事实，"
                        "再基于检索结果作答；回答中如引用外部信息，请保留引用标记。"
                    ),
                }
            )
        max_tokens = 1600 if thinking else 700

        try:
            if not llm.live:
                raise LLMError("未配置模型 Key")
            async for kind, delta in llm.chat_stream_events(
                messages,
                temperature=0.7,
                max_tokens=max_tokens,
                model=model,
                trace=question_id,
                thinking=thinking,
                web_search=web,
                category=judge.ANSWER_CATEGORY.get(level, "答案"),
            ):
                if kind == "sources":
                    try:
                        payload = json.loads(delta)
                    except Exception:  # noqa: BLE001
                        payload = {}
                    raw_sources = payload.get("annotations") or []
                    sources: list[dict] = []
                    seen_urls: set[str] = set()
                    for item in raw_sources:
                        loc = item.get("url_citation") if isinstance(item, dict) else None
                        loc = loc if isinstance(loc, dict) else item
                        url = (loc or {}).get("url")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        sources.append({"title": (loc.get("title") or url)[:120], "url": url})
                    if sources:
                        if self._current and self._current["id"] == question_id:
                            self._current["sources"] = sources
                            self._current["web_used"] = True
                        await self._broadcast(
                            {"type": "answer_sources", "id": question_id, "items": sources}
                        )
                    continue
                if kind == "reasoning":
                    reasoning += delta
                    await self._broadcast(
                        {"type": "answer_reasoning", "id": question_id, "text": delta}
                    )
                else:
                    text += delta
                    streamed = True
                    await self._broadcast(
                        {"type": "answer_delta", "id": question_id, "text": delta}
                    )
        except asyncio.CancelledError:
            # 面试官接着说下一句时，这场话术会被取消：把已经流出来的部分存进卡片，
            # 否则界面看得到半截答案，记录里却是空的。
            partial = text.strip()
            if partial and self._current and self._current["id"] == question_id:
                self._current["answer"] = partial
                self._current["answers"] = {"standard": partial}
                log.info("话术被下一题打断，已保存半截答案: id=%s %d字", question_id, len(partial))
            raise
        except LLMError as exc:
            log.warning("参考答案生成失败，使用模板兜底: %s", exc)
            self.degraded = True
            text = ""
            if streamed:
                await self._broadcast({"type": "answer_reset", "id": question_id})

        if text.strip() and judge.relevance(question, text, focus) < 0.05:
            await self._broadcast({"type": "answer_reset", "id": question_id})
            text = ""
            try:
                text = await judge.compose_answer(
                    self.cfg, question, focus, self.history, level, references, memory_hint,
                    trace=question_id,
                )
            except LLMError:
                text = ""
            if text.strip():
                for index in range(0, len(text), 8):
                    chunk = text[index : index + 8]
                    await self._broadcast({"type": "answer_delta", "id": question_id, "text": chunk})
                    await asyncio.sleep(0.015)

        if not text.strip():
            log.warning("参考答案为空，用模板兜底: id=%s 问题=%r", question_id, question)
            template = _template_answer(question, level)
            for index in range(0, len(template), 6):
                chunk = template[index : index + 6]
                text += chunk
                await self._broadcast({"type": "answer_delta", "id": question_id, "text": chunk})
                await asyncio.sleep(0.02)

        # 模型没标到的英文术语补上读音；已有注音的不会重复
        text = await _polish(text)

        answer_ms = int((time.perf_counter() - answer_started) * 1000)
        if self._current and self._current["id"] == question_id:
            self._current["answer"] = text
            self._current["duration_ms"] = answer_ms
            if reasoning.strip():
                self._current["reasoning"] = reasoning.strip()

        relevance_score = judge.relevance(question, text, focus)
        log.info(
            "参考答案完成: id=%s 字数=%d 相关度=%.2f 降级=%s 思考=%s 思考字数=%d",
            question_id,
            len(text.strip()),
            relevance_score,
            self.degraded,
            thinking,
            len(reasoning),
        )

        await self._broadcast(
            {
                "type": "answer_done",
                "id": question_id,
                "text": text,
                "degraded": self.degraded,
                "relevance": round(relevance_score, 2),
                "thinking": thinking,
                "web": web,
                "modes": modes,
                "duration_ms": answer_ms,
            }
        )

        if not text.strip():
            await variant_task
            return

        variants: dict[str, str] = {"standard": text.strip()}
        brief_text, deep_text = await variant_task
        if brief_text:
            variants["brief"] = brief_text
        if deep_text:
            variants["deep"] = deep_text

        if self._current and self._current["id"] == question_id:
            self._current["answers"] = variants
            self._current["level"] = "standard"
            self._current["modes"] = modes

        total_ms = int((time.perf_counter() - answer_started) * 1000)
        if self._current and self._current["id"] == question_id:
            self._current["total_ms"] = total_ms

        log.info(
            "三版齐全: id=%s %s（总耗时 %sms）",
            question_id,
            {name: len(value) for name, value in variants.items()},
            total_ms,
        )
        await self._broadcast(
            {
                "type": "answer_variants",
                "id": question_id,
                "answers": variants,
                "modes": modes,
                "duration_ms": total_ms,
            }
        )


def _template_answer(question: str, level: str) -> str:
    topic = question.strip().rstrip("？?。 ，,")
    topic = topic[:26] + ("…" if len(topic) > 26 else "")
    if level == "brief":
        return f"关于「{topic}」：我做过，核心是先定位问题、再用成熟方案落地，最后用线上指标验证效果。"
    if level == "deep":
        return (
            f"关于「{topic}」，我当时是这么处理的。背景是业务量在一年里涨了数倍，我负责其中一段核心链路，"
            "主要矛盾是延迟和稳定性。我先把链路拆成可观测的几段，定位到最慢的一段，换成更合适的方案，"
            "又补了灰度和监控兜住风险。结果是延迟明显下降，扩容时也没有推倒重来。"
            "如果重来一次，我会更早把压测和监控铺上，少走一段弯路。"
        )
    return (
        f"关于「{topic}」，我确实有实践。当时的瓶颈是链路里一段响应不稳，"
        "我先把链路拆成可观测的几段，定位到最慢的一段，换成更合适的方案，"
        "再把灰度和回滚补上。最后响应时间明显下降，线上也稳定住了。"
    )
