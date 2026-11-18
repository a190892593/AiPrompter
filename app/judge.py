"""意图识别：判断面试官此刻是否在提问，并归纳出他真正想问什么。

真实面试的语音转写有三个坑，意图识别必须处理：
1. 问题被拆成好几句，中间夹着"那个"、"我是说如果哈"；
2. 中间插进接电话、旁人说话、喂喂听得到吗；
3. 带问号的引子（"你们之前不是说做了个订单系统嘛？"）根本不需要回答。

所以这里不看"最后一句"，而是看最近若干句，输出 type/question/focus/spans/reason。
没有模型时退回启发式规则，保证链路不中断。
"""

from __future__ import annotations

from datetime import datetime
import asyncio
import difflib
import logging
import re
import time

from .config import settings
from . import prompts
from .llm import LLMError, llm
from .schemas import SessionConfig

log = logging.getLogger("interview.judge")

# 常见领域术语（给"意图识别/问题重写"做题面提示，避免模型把误识别的术语猜歪）。
# 只放通用技术词；公司内部系统名这类模型猜不到的，靠模型在上下文里自己判断，
# 不再维护可编辑的别名表。
_DOMAIN_TERMS_TEXT = (
    "RAG、LLM、Agent、LangChain、LangGraph、LlamaIndex、Milvus、Faiss、Chroma、HNSW、"
    "IVF-Flat、BM25、embedding、rerank、recall、NDCG、LoRA、PEFT、DeepSpeed、vLLM、"
    "SGLang、Transformer、token、prompt、pipeline、baseline、latency、throughput"
)

# ---------------------------------------------------------------- 提示词

LENGTH_RULE = {
    "brief": "总长不超过 40 字（英文注音括号不计入），像关键词提示，只保留结论和关键数据",
    "standard": "总长 80-120 字（英文注音括号不计入），能直接念出来",
    "deep": "总长 220-280 字（英文注音括号不计入），用 STAR 结构：情境-任务-行动-结果",
}

# 调用日志里的“调用分类”
ANSWER_CATEGORY = {
    "brief": "答案·极简",
    "standard": "答案·标准",
    "deep": "答案·深度",
}



# ---------------------------------------------------------------- 启发式

# 离场 / 环境噪音：不打断原问题
ASIDE_PATTERNS = [
    "接个电话", "接电话", "稍等一下", "等一下啊", "喂喂", "喂？", "喂?", "听得见吗", "听得到吗",
    "听不见", "听不清", "信号", "网卡", "卡了", "掉线", "断了", "没声音", "再调一下",
    "你先说", "我这边有点吵", "旁边", "有人叫我", "不好意思啊", "稍等稍等", "回个消息",
    "网有点卡", "网络不好", "听不到", "再说一遍", "刚刚没听清", "我看看啊", "我这边",
]

# 收尾语：明确不再提问
CLOSING_PATTERNS = [
    "没什么要问", "没什么问题", "没有问题了", "先到这儿", "先这样", "后面再聊", "后面细聊",
    "今天先", "辛苦你", "我没什么想问", "差不多就", "先聊到", "后面再聊", "回头聊", "先这样吧",
]

# 引子式反问：带问号但不需要回答
LEADIN_PATTERNS = [
    "不是说", "对吧", "是吧", "你懂我意思吧", "明白吧", "懂我意思吗",
    "对不对", "是不是这样", "好不好",
]

COMMAND_PATTERNS = [
    "说说", "说一下", "介绍一下", "讲讲", "讲一下", "讲一讲", "聊聊", "谈一谈",
    "你来说", "举个例子", "手写", "写一下", "画一下", "设计一下", "做个", "做一下",
    "整理", "发我", "发到", "列一下", "过一遍",
]

WH_WORDS = [
    "什么", "怎么", "为什么", "如何", "多少", "哪", "哪些", "是否", "能不能",
    "会不会", "有没有", "多久", "多大", "区别", "对比", "啥", "咋", "怎样", "几次", "几种",
    "谁",
]

CHOICE_MARKERS = ["还是说", "还是", "或者", "要不要", "是不是", "还是说"]

CHITCHAT_HINTS = ["嗯嗯", "好的好的", "行", "明白", "收到", "那就好", "哈哈", "嗯", "好"]

INCOMPLETE_TAIL = ["那个", "这个", "我是说", "如果", "就是说", "然后", "而且", "因为"]

# 反问/质疑标记：面试官在验证候选人说的是否靠谱
CHALLENGE_MARKERS = [
    "你确定", "确定吗", "真的吗", "真的假的", "凭什么", "有数据吗", "有证据吗", "怎么证明",
    "能行吗", "可行吗", "靠得住吗", "靠谱吗", "不会吧", "那岂不是", "难道", "为什么不用",
    "为什么不", "这也能", "你是不是在", "有没有考虑过", "万一", "那如果不行",
]

# 追问承接词：最后一句出现这些，且前面已有问答，就按追问处理
FOLLOWUP_MARKERS = [
    "那如果", "那要是", "那这个", "那这样", "那你们", "那它", "这个呢", "这块呢",
    "刚才你", "你刚才", "你刚说", "你说的", "除了", "还有呢", "还有别的", "还有啥",
    "为什么", "具体多少", "具体怎么", "再详细", "继续", "再多说", "然后呢", "怎么保证",
]

FILLERS = ["嗯", "啊", "那个", "这个", "就是说", "我想问一下", "我问一下", "其实"]


def _clean(text: str) -> str:
    out = text.strip()
    for word in FILLERS:
        out = out.replace(word, "")
    return out.strip(" ，,、")


def _is_aside(text: str) -> bool:
    if any(pattern in text for pattern in ASIDE_PATTERNS):
        return True
    # 转写里常用括号标注动作，如"（对门口）"、"（手机响）"，开头是动作基本是在跟别人说话
    match = re.match(r"^[（(][^）)]{1,24}[）)]", text.strip())
    if match:
        rest = text.strip()[match.end() :].strip()
        if len(rest) <= 40 and not any(word in rest for word in WH_WORDS):
            if not rest.rstrip().endswith(("？", "?")):
                return True
    return False


def _is_closing(text: str) -> bool:
    """收尾语：明确不再提问。但带问号的句子不算（"先聊到这，你怎么理解…？"）。"""
    if text.rstrip().endswith(("？", "?")):
        return False
    return any(pattern in text for pattern in CLOSING_PATTERNS)


def _is_leadin(text: str) -> bool:
    if any(pattern in text for pattern in LEADIN_PATTERNS):
        # "不是说...嘛" 这种是引子；但如果同时有实义疑问词，仍然算提问
        return not any(word in text for word in WH_WORDS)
    return False


def _looks_incomplete(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.endswith(("，", ",", "、", "……", "...")):
        return True
    if len(stripped) <= 12 and stripped.endswith(tuple(INCOMPLETE_TAIL)):
        return True
    if any(stripped.endswith(word) for word in ("如果", "我是说", "那个", "就是说")):
        return True
    return False


def heuristic_classify(history: list[dict]) -> dict:
    """没有模型时的兜底：按"是否真的需要候选人开口"逐层排除。"""
    interviewer = [item for item in history if item["channel"] == "interviewer"]
    if not interviewer:
        return {"type": "chitchat", "question": "", "focus": [], "spans": 1, "confidence": 0.2, "reason": "无内容"}

    last = interviewer[-1]["text"].strip()
    if not last:
        return {"type": "chitchat", "question": "", "focus": [], "spans": 1, "confidence": 0.2, "reason": "空句子"}

    if _is_aside(last):
        return {
            "type": "aside",
            "question": "",
            "focus": [],
            "spans": 1,
            "confidence": 0.8,
            "reason": "接电话或环境噪音",
        }

    if any(pattern in last for pattern in CLOSING_PATTERNS) and _is_closing(last):
        return {"type": "statement", "question": "", "focus": [], "spans": 1, "confidence": 0.75, "reason": "收尾语，不再提问"}

    if _is_leadin(last):
        return {
            "type": "statement",
            "question": "",
            "focus": [],
            "spans": 1,
            "confidence": 0.75,
            "reason": "引子式反问，不需要回答",
        }

    # 合并被拆开的问题：最后一句不完整时，往前拼最近 2 句
    merged = last
    spans = 1
    if _looks_incomplete(last):
        for previous in reversed(interviewer[:-1][-2:]):
            candidate = f"{previous['text'].strip()}{merged}"
            if len(candidate) > 60:
                break
            merged = candidate
            spans += 1
            if not _looks_incomplete(merged):
                break

    if _is_aside(merged):
        return {"type": "aside", "question": "", "focus": [], "spans": spans, "confidence": 0.8, "reason": "接电话或环境噪音"}

    if any(word in merged for word in COMMAND_PATTERNS):
        kind = "instruction"
    elif _looks_like_question(merged):
        kind = "question"
    elif len(merged) <= 14 and any(hint in merged for hint in CHITCHAT_HINTS):
        return {"type": "chitchat", "question": "", "focus": [], "spans": spans, "confidence": 0.5, "reason": "短附和"}
    else:
        return {"type": "statement", "question": "", "focus": [], "spans": spans, "confidence": 0.6, "reason": "陈述，未提问"}

    if len(merged) < 6:
        return {"type": "chitchat", "question": "", "focus": [], "spans": spans, "confidence": 0.2, "reason": "过短"}

    return {
        "type": kind,
        "question": (_clean_question(merged) or _clean(merged))[:60],
        "focus": [],
        "spans": spans,
        "confidence": 0.72,
        "reason": "启发式疑问词匹配",
    }


def _looks_like_question(text: str) -> bool:
    if any(word in text for word in WH_WORDS):
        return True
    if any(word in text for word in CHOICE_MARKERS):
        return True
    # 去掉尾部标点后再看语气词，"能保证一致吗？" 这种才算提问
    stripped = text.rstrip("？?。！!…~ 　")
    return stripped.endswith(("吗", "呢", "么", "不"))



# ------------------------------------------------- 问题重写（二次纠偏）


_FILLER_PREFIXES = (
    "嗯", "啊", "呃", "额", "那个", "这个", "就是", "其实", "然后", "那么",
    "我想问一下", "我问一下", "想问一下", "请问一下", "请问", "我是说", "就是说",
)
_FILLER_SUFFIXES = ("啊", "呢", "吧", "哈", "嘛", "好不好", "对吧", "是吧", "对不对", "是不是")
_GENERIC_QUESTIONS = {
    "说说吧", "说说", "讲讲", "讲讲吧", "谈谈", "说一下", "介绍一下", "详细说说",
    "具体说说", "详细讲讲", "具体讲讲", "仔细说说", "展开说说", "举个例子", "还有吗",
}


def _clean_question(text: str) -> str:
    """去掉口头禅/语气词，整理成一句干净的问题。"""
    q = (text or "").strip()
    if not q:
        return ""
    q = re.sub(r"^[\s，,、。；;：:]+", "", q)
    changed = True
    while changed and q:
        changed = False
        for prefix in _FILLER_PREFIXES:
            if q.startswith(prefix) and len(q) > len(prefix) + 1:
                q = q[len(prefix) :].lstrip("，,、 ")
                changed = True
    for suffix in _FILLER_SUFFIXES:
        if q.endswith(suffix) and len(q) > len(suffix) + 3:
            q = q[: -len(suffix)].rstrip("，,、 ")
    q = re.sub(r"\s{2,}", " ", q).strip(" ，,、")
    return q


def _looks_generic(question: str) -> bool:
    stripped = re.sub(r"[？?。！!…\s]", "", question or "")
    if not stripped:
        return True
    if stripped in _GENERIC_QUESTIONS:
        return True
    return len(stripped) < 5


def _fallback_question(history: list[dict]) -> str:
    """模型没给出问题时：用最近几句面试官原话拼一句可用的。"""
    parts: list[str] = []
    for item in reversed(history):
        if item.get("channel") != "interviewer":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.insert(0, text)
        if len("".join(parts)) >= 40 or len(parts) >= 2:
            break
    raw = "".join(parts)
    cleaned = _clean_question(raw)
    return (cleaned or raw)[:60]


_REFINE_SYSTEM = (
    "你是面试问题重写器。给你面试官最近几句原话（可能有口头禅、碎片、自纠、指代、"
    "术语误识），结合上下文写出面试官真正想问的那个问题。要求：自包含、可直接回答、"
    "≤40 字、不要空泛（禁止“说说吧”“详细讲讲”）。只输出 JSON：{\"question\": \"...\"}"
)


async def _refine_question(cfg: SessionConfig, history: list[dict], trace: str = "") -> str:
    """二次纠偏（问题重写）：第一次归纳为空/空泛时，把碎片/指代/自纠还原成自包含问题。"""
    if not llm.live:
        return ""
    lines = _format_history(history)
    term_list = _DOMAIN_TERMS_TEXT
    messages = [
        {"role": "system", "content": _REFINE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【个人简历】{(cfg.resume or '').strip()[:1500] or '未提供'}\n【领域术语】{term_list}\n\n"
                f"【最近对话】\n{lines}\n\n"
                "请只输出面试官真正想问的问题。"
            ),
        },
    ]
    try:
        data = await asyncio.wait_for(
            llm.chat_json(
                messages,
                temperature=0.0,
                max_tokens=200,
                trace=trace,
                thinking=False,
                category="问题重写",
            ),
            timeout=8,
        )
    except asyncio.TimeoutError:
        log.warning("问题重写超时（>8s），用原话兜底")
        return ""
    except LLMError as exc:
        log.warning("问题重写失败: %s", exc)
        return ""
    except Exception:  # noqa: BLE001 - 纠偏失败不能影响主链路
        log.exception("问题重写异常")
        return ""
    return _clean_question(str(data.get("question") or ""))



_VERIFY_SYSTEM = (
    "你是意图识别的复核员，目标只有一个：避免给候选人一个错误的问题。判断："
    "① 这是不是面试官此刻真正需要候选人回答的问题；"
    "② 是否忠实于原话（没有把旁白/引子/陈述当问题，没有曲解主题、对象或数字）；"
    "③ 如果最后一句是接电话/离场/和旁人说话/抱怨网络等旁白，即使前面有提问，也判 ok=false"
    "（那个问题上一轮已经出过卡，重复出卡就是错题）。"
    "只有这三种情况判 ok=false；补全省略或推断语境（例如把“80 毫秒”补成“从多少降到 80 毫秒”）不算不忠实，"
    "如果意图成立但表述有偏差，返回修正后的问题。只输出 JSON："
    "{\"ok\": true/false, \"question\": \"修正后的问题（ok 时可留空）\", \"reason\": \"≤20 字\"}"
)


async def _verify(
    cfg: SessionConfig, history: list[dict], question: str, trace: str = ""
) -> dict | None:
    """二次校验：低置信/合并多句/被纠偏过的问题再复核一遍。失败返回 None（不拦截）。"""
    if not settings.intent_verify or not llm.live or not (question or "").strip():
        return None
    lines = _format_history(history)
    messages = [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【岗位】{cfg.role}\n\n【最近对话】\n{lines}\n\n"
                f"【待确认问题】{question}\n\n"
                "请复核：这个问题是否成立、是否忠实于原话；最后一句是不是旁白。"
            ),
        },
    ]
    try:
        data = await asyncio.wait_for(
            llm.chat_json(
                messages,
                temperature=0.0,
                max_tokens=200,
                trace=trace,
                thinking=False,
                category="二次复核",
            ),
            timeout=settings.intent_verify_timeout,
        )
    except asyncio.TimeoutError:
        log.warning("意图识别二次校验超时（>%.1fs），放行", settings.intent_verify_timeout)
        return None
    except LLMError as exc:
        log.warning("意图识别二次校验调用失败: %s", exc)
        return None
    except Exception:  # noqa: BLE001 - 校验失败不拦主链路
        log.exception("意图识别二次校验异常")
        return None

    raw_ok = data.get("ok")
    if isinstance(raw_ok, str):
        ok = raw_ok.strip().lower() in ("true", "1", "yes", "是", "对")
    elif raw_ok is None:
        ok = True
    else:
        ok = bool(raw_ok)
    return {
        "ok": ok,
        "question": str(data.get("question") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
    }


# ---------------------------------------------------------------- 模型意图识别


def _format_history(history: list[dict], limit: int = 20) -> str:
    """渲染最近对话。每行带时间戳，停顿超过 5 秒也标出来——模型能据此判断
    "这是同一句话的延续"还是"隔了一会儿又起新话题"。

    默认给 20 句：判题需要跨多轮理解指代、追问和被打断的问题；之前只给 7 句，
    稍长一点的面试官铺垫就会被截掉。
    """
    lines = []
    previous_ts = 0.0
    for item in history[-limit:]:
        ts = float(item.get("ts") or 0.0)
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--:--"
        gap = ""
        if previous_ts and ts - previous_ts >= 5:
            gap = f"（停顿 {int(ts - previous_ts)}s）"
        who = "面试官" if item["channel"] == "interviewer" else "候选人"
        lines.append(f"[{stamp}]{gap} {who}：{item['text'].strip()}")
        if ts:
            previous_ts = ts
    return "\n".join(lines) or "（无）"


async def generate_question(
    cfg: SessionConfig,
    history: list[dict],
    asked: list[str] | None = None,
    trace: str = "",
    angle: str = "",
    hint: str = "",
) -> str:
    """「简历 + 追问」模式：先从简历开问，之后顺着候选人的回答继续反问/追问。

    追问上限不依赖外部计数器：直接由「已经问过的问题」数量推导——
    每 4 句为一组（1 个主问题 + 3 个追问），到第 5 句强制换新话题。
    这样 generate_question 是纯函数，仿真器不需要保存轮次状态。
    """
    if not llm.live:
        return ""
    resume = (cfg.resume or "").strip()
    background = (cfg.background or "").strip()
    if not resume and not background:
        return ""
    asked_list = list(asked or [])
    asked_text = "\n".join(f"- {item}" for item in asked_list[-30:]) or "（暂无）"
    topic_round = len(asked_list) % 4
    force_new = len(asked_list) >= 4 and topic_round == 0
    last_answer = next(
        (item["text"] for item in reversed(history) if item["channel"] == "candidate"), ""
    ).strip()
    if force_new:
        # 强制换话题时不把上一句回答喂进去，避免模型继续抓住同一件事追问
        last_answer = ""
    messages = [
        {
            "role": "system",
            "content": (
                "你是正在面试候选人的面试官。你会从候选人简历里的项目开始问，"
                "然后像真人一样抓住他刚才回答里的细节继续追问或反问。"
                "问题要口语、具体、一次只问一个点；只输出这一句问题，不要编号、不要解释、不要 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"【岗位】{cfg.role}\n"
                f"【岗位职责/要求】\n{background[:2500] or '未提供'}\n"
                f"【候选人简历】\n{resume[:5000]}\n"
                f"【最近对话】\n{_format_history(history, 12)}\n"
                f"【候选人刚才的回答】{last_answer[:600] or '（还没回答，这是开场提问）'}\n"
                f"【本轮切入点】{angle or '由你选择'}\n"
                f"【本轮优先围绕的简历片段】{hint[:600] or '简历整体'}\n"
                f"【已经问过的问题】\n{asked_text}\n\n"
                + (
                    "【本轮要求】已经围绕上一个话题追问满 3 轮，本轮必须从简历里换一个项目/经历，"
                    "禁止再追问刚才那件事；禁止用「刚才你提到」「前面说的」开头，"
                    "如果简历里没有别的项目，就换一个完全不同的考察维度（团队、失败、成本、重做），"
                    "不要再问上一句的细节。把这一问当作新话题的开场。\n"
                    if force_new
                    else (
                        f"【本轮要求】当前话题第 {topic_round + 1} 句；同一话题最多 4 句"
                        "（1 个主问题 + 3 个追问），到第 4 句后必须换新话题，禁止无限追问同一件事。\n"
                    )
                )
                + "【这轮怎么问】\n"
                "1. 候选人还没回答时：从【本轮优先围绕的简历片段】入手，按【本轮切入点】提问，"
                "问清他负责什么、规模多大、结果如何；不要每次都拿简历里同一个指标/项目开场。\n"
                "2. 只要候选人答过，下一问必须是针对他刚才那句话的追问或反问，扣住一个具体细节：\n"
                "   - 要数字和口径：这个指标在哪测的、按什么口径算、基线是多少；\n"
                "   - 要取舍：为什么这么选、当时对比过什么、放弃的方案差在哪；\n"
                "   - 要边界和失败：流量/数据量再大一个量级会怎样、哪里先崩、出过什么事故；\n"
                "   - 要反驳：他说的和简历/上一句对不上时，直接问回去。\n"
                "3. 他答得空泛、绕开、只给结论时，同一个话题换个角度继续追问，但同一话题最多追问 3 轮。\n"
                "4. 追问满 3 轮后，必须用「刚才你提到 X……」或直接从简历换到下一个项目，问一个新问题。\n"
                "5. 不要问简历里没有的经历，不要重复已经问过的问题，不要一次问两件事。只输出问题本身。"
            ),
        },
    ]
    text = ""
    async for delta in llm.chat_stream(
        messages,
        temperature=0.85,
        max_tokens=220,
        model=settings.llm_model,
        trace=trace,
        thinking=False,
        category="简历提问",
    ):
        text += delta
    return _clean_question(text)


def _format_examples(examples: list[dict]) -> str:
    """把历史纠错样本渲染成 few-shot，越像的排越前。"""
    if not examples:
        return ""
    lines = ["【历史纠错示例】（这些是同类场景之前的正确判定，优先参考）"]
    for item in examples:
        lines.append(f"面试官：「{item['last_text']}」→ 应判 {item['correct_type']}：{item.get('corrected_question') or '不需要回答'}")
    return "\n".join(lines) + "\n\n"


def _clamp_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _recall(examples: list[dict] | None, last_text: str) -> dict | None:
    """命中历史纠错：同一句话再出现时直接沿用人工判定。

    这是"学到的经验"，比规则和模型都准（因为是人亲手标的）。
    """
    for item in examples or []:
        correct_type = str(item.get("correct_type") or "").strip()
        if not correct_type:
            continue
        ratio = difflib.SequenceMatcher(None, last_text, str(item.get("last_text") or "")).ratio()
        if ratio >= 0.9:
            return {
                "type": correct_type,
                "question": str(item.get("corrected_question") or "").strip(),
                "focus": [],
                "spans": 1,
                "confidence": 0.95,
                "reason": "命中历史纠错",
            }
    return None


def _fast_verdict(history: list[dict]) -> dict | None:
    """明确的旁白/收尾/引子/附和直接判定，不花模型调用。只快判“不回答”，不生成问题。"""
    last_text = next(
        (item["text"] for item in reversed(history) if item["channel"] == "interviewer"), ""
    )
    stripped = last_text.strip()
    if not stripped or len(stripped) > 24:
        return None
    has_question_word = any(word in stripped for word in WH_WORDS)
    ends_question = stripped.rstrip().endswith(("？", "?"))
    if _is_aside(stripped) and not has_question_word and not ends_question:
        return {"type": "aside", "question": "", "focus": [], "spans": 1,
                "followup": False, "challenge": False, "confidence": 0.85, "reason": "快速通道：旁白"}
    if _is_leadin(stripped):
        return {"type": "statement", "question": "", "focus": [], "spans": 1,
                "followup": False, "challenge": False, "confidence": 0.8, "reason": "快速通道：引子反问"}
    if len(stripped) <= 8 and any(hint in stripped for hint in CHITCHAT_HINTS):
        return {"type": "chitchat", "question": "", "focus": [], "spans": 1,
                "followup": False, "challenge": False, "confidence": 0.7, "reason": "快速通道：短附和"}
    return None


async def warmup() -> None:
    """预热意图识别的提示词前缀 + HTTP 连接，降低第一场面试的首字延迟。"""
    if not llm.live:
        return
    try:
        user = prompts.render(
            "judge.user",
            role="预热",
            jd="预热",
            resume="预热",
            terms=_DOMAIN_TERMS_TEXT,
            lines="[00:00:00] 面试官：预热",
            examples="",
        )
        await llm.chat(
            [
                {"role": "system", "content": prompts.render("judge.system")},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=1,
            trace="warmup",
            thinking=False,
            category="预热",
        )
        log.info("意图识别提示词前缀已预热")
    except Exception:  # noqa: BLE001 - 预热失败不影响服务
        log.debug("意图识别预热失败", exc_info=True)


async def classify(
    cfg: SessionConfig,
    history: list[dict],
    examples: list[dict] | None = None,
    trace: str = "",
    verify: bool = True,
    refine: bool = True,
) -> dict:
    """返回 {type, question, focus, spans, confidence, reason}。"""
    last_text = next(
        (item["text"] for item in reversed(history) if item["channel"] == "interviewer"), ""
    )
    recalled = _recall(examples, last_text)
    if recalled is not None:
        log.info(
            "意图识别命中历史纠错 trace=%s 文本=%r → 类型=%s",
            trace or "-",
            last_text[:60],
            recalled["type"],
        )
        return recalled

    # 确定性快速通道：明确不是提问的短句直接返回，不调模型（省 1s+，不影响该回答的判定）
    fast = _fast_verdict(history)
    if fast is not None:
        log.info("意图识别快速通道: 类型=%s 文本=%r", fast["type"], last_text[:40])
        return fast

    if not llm.live:
        verdict = heuristic_classify(history)
        log.info("未配置模型 Key，走启发式意图识别: 类型=%s", verdict["type"])
        return verdict

    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": prompts.render("judge.system")},
                {
                    "role": "user",
                    "content": prompts.render(
                        "judge.user",
                        role=cfg.role,
                        jd=cfg.background.strip()[:1500] or "未提供",
                        resume=(cfg.resume or "").strip()[:3000] or "未提供",
                        terms=_DOMAIN_TERMS_TEXT,
                        lines=_format_history(history),
                        examples=_format_examples(examples or []),
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=900 if settings.judge_thinking else 220,
            trace=trace,
            thinking=bool(settings.judge_thinking),
            category="意图识别主判",
        )
    except LLMError as exc:
        log.warning("意图识别调用失败 trace=%s，降级启发式: %s", trace or "-", exc)
        verdict = heuristic_classify(history)
        log.info("启发式兜底结论: 类型=%s 置信度=%.2f 原因=%s", verdict["type"], verdict["confidence"], verdict["reason"])
        return verdict

    kind = str(data.get("type") or "").strip().lower()
    if kind not in {"question", "instruction", "statement", "chitchat", "aside", "incomplete"}:
        kind = "statement"

    spans_raw = data.get("spans")
    try:
        spans = int(spans_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        spans = 1

    raw_followup = data.get("followup")
    if isinstance(raw_followup, str):
        followup = raw_followup.strip().lower() in ("true", "1", "yes", "是")
    else:
        followup = bool(raw_followup)
    # 模型漏标时用承接词兜底：前面已有"面试官提问 → 候选人回答"，最后一句又带承接词 → 追问
    if not followup and kind in ("question", "instruction"):
        try:
            seen_interviewer = False
            seen_candidate_after = False
            for item in history:
                if item.get("channel") == "interviewer":
                    seen_interviewer = True
                elif seen_interviewer and (item.get("text") or "").strip():
                    seen_candidate_after = True
            last_raw = next(
                (item.get("text", "") for item in reversed(history) if item.get("channel") == "interviewer"),
                "",
            )
            fresh_topic = ("团队", "管理", "薪资", "职业规划", "为什么来", "选择我们", "公司")
            starts_followup = last_raw.strip().startswith(("那", "那你们", "那如果", "那要是", "这个", "这块"))
            has_marker = any(marker in last_raw for marker in FOLLOWUP_MARKERS)
            is_fresh = any(word in last_raw for word in fresh_topic)
            if seen_candidate_after and (has_marker or (starts_followup and not is_fresh)):
                followup = True
                log.info("追问启发式命中: %r -> followup=true", last_raw[:50])
        except Exception:  # noqa: BLE001 - 推断失败不影响主判定
            pass
    raw_challenge = data.get("challenge")
    if isinstance(raw_challenge, str):
        challenge = raw_challenge.strip().lower() in ("true", "1", "yes", "是")
    else:
        challenge = bool(raw_challenge)
    if not challenge and kind in ("question", "instruction"):
        last_for_challenge = next(
            (item.get("text", "") for item in reversed(history) if item.get("channel") == "interviewer"),
            "",
        )
        if any(marker in last_for_challenge for marker in CHALLENGE_MARKERS):
            challenge = True
            log.info("反问启发式命中: %r -> challenge=true", last_for_challenge[:50])
    # 确定性护栏：最后一句是接电话/离场/网络噪音时，即使模型判成提问也压回 aside
    # （那个问题上一轮已经出过卡，重复出卡就是错题）
    last_interviewer = next(
        (item.get("text", "") for item in reversed(history) if item.get("channel") == "interviewer"),
        "",
    )
    stripped_last = last_interviewer.strip()
    is_short_aside = (
        len(stripped_last) <= 24
        and not any(word in stripped_last for word in WH_WORDS)
        and not stripped_last.rstrip().endswith(("？", "?"))
    )
    if kind in ("question", "instruction") and is_short_aside and _is_aside(stripped_last):
        log.info("末句旁白护栏: %r -> aside（原判 %s）", stripped_last[:50], kind)
        kind = "aside"
        data = {"type": "aside", "question": "", "focus": [], "confidence": 0.85, "reason": "末句旁白，问题上一轮已处理"}

    raw_queries = data.get("search_queries") or []
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    search_queries: list[str] = []
    for item in raw_queries if isinstance(raw_queries, list) else []:
        text_value = str(item).strip()
        if 2 <= len(text_value) <= 40 and text_value not in search_queries:
            search_queries.append(text_value)
    search_queries = search_queries[:3]

    corrected_raw = str(data.get("corrected") or "").strip()
    corrected_text = ""
    if corrected_raw and corrected_raw != last_text:
        # 只接受长度接近的整句改写，防止模型乱编
        if 0.7 * len(last_text) <= len(corrected_raw) <= 1.4 * len(last_text):
            corrected_text = corrected_raw

    result = {
        "type": kind,
        "corrected": corrected_text,
        "search_queries": search_queries,
        "question": str(data.get("question") or "").strip(),
        "focus": [str(x) for x in (data.get("focus") or [])][:3],
        "spans": max(1, min(3, spans)),
        "followup": followup,
        "challenge": challenge,
        "confidence": _clamp_confidence(data.get("confidence")),
        "reason": str(data.get("reason") or "").strip(),
    }
    # 纠偏：先做确定性清洗；question/instruction 缺问题或空泛时，再做一次上下文纠偏
    if result["type"] in ("question", "instruction"):
        result["question"] = _clean_question(result["question"])
        refined_used = False
        if refine and _looks_generic(result["question"]):
            refined = await _refine_question(cfg, history, trace)
            if refined and not _looks_generic(refined):
                log.info("问题重写（二次纠偏）: %r -> %r", result["question"], refined)
                result["question"] = refined
                result["reason"] = (result["reason"] + "；问题重写").strip("；") if result["reason"] else "问题重写"
                refined_used = True
            elif not result["question"]:
                result["question"] = _fallback_question(history)

        # 二次校验：低置信度 / 合并多句 / 被纠偏过的问题，宁可不出卡也不出错题
        needs_verify = (
            result["confidence"] < settings.intent_verify_below
            or result["spans"] >= 2
            or refined_used
        )
        if verify and needs_verify:
            verified = await _verify(cfg, history, result["question"], trace)
            if verified is not None:
                result["verify"] = verified
                if verified.get("ok") is False:
                    log.info(
                        "二次校验未通过 trace=%s 问题=%r 原因=%s",
                        trace or "-",
                        result["question"][:50],
                        verified.get("reason") or "-",
                    )
                else:
                    corrected = _clean_question(verified.get("question") or "")
                    if corrected and not _looks_generic(corrected):
                        log.info("二次校验修正问题: %r -> %r", result["question"], corrected)
                        result["question"] = corrected

    log.debug(
        "意图识别模型原始输出 trace=%s type=%r question=%r confidence=%r spans=%r",
        trace or "-",
        data.get("type"),
        data.get("question"),
        data.get("confidence"),
        spans_raw,
    )
    return result


# ---------------------------------------------------------------- 参考答案


def answer_messages(
    cfg: SessionConfig,
    question: str,
    focus: list[str],
    history: list[dict],
    level: str,
    references: list[dict] | None = None,
    memory: str = "",
    prev_qa: str = "",
    mode_note: str = "",
) -> list[dict[str, str]]:
    background = cfg.background.strip()[:2000]
    # 个人简历是新面试的必填项，和岗位 JD 一起给模型：回答要能对上真实经历
    resume = (cfg.resume or "").strip()
    if resume:
        background = f"{background}\n\n【我的简历】\n{resume[:3000]}".strip()
    return [
        {"role": "system", "content": prompts.render("answer.system")},
        {
            "role": "user",
            "content": prompts.render(
                "answer.user",
                role=cfg.role,
                background=background or "未提供",
                memory=memory or "（暂无历史记录）",
                question=question,
                focus="、".join(focus) or "（未识别）",
                prev_qa=prev_qa or "（无，这不是追问）",
                mode_note=mode_note or "正常提问，直接回答",
                reference="\n".join(
                    f"问：{item.get('question', '')}\n答：{item.get('answer', '')}"
                    for item in (references or [])
                )
                or "（无）",
                context=_format_history(history, 16),
                length_rule=LENGTH_RULE.get(level, LENGTH_RULE["standard"]),
            ),
        },
    ]


async def compose_answer(
    cfg: SessionConfig,
    question: str,
    focus: list[str],
    history: list[dict],
    level: str = "standard",
    references: list[dict] | None = None,
    memory: str = "",
    trace: str = "",
    prev_qa: str = "",
    mode_note: str = "",
    thinking: bool = False,
    web_search: bool = False,
) -> str:
    """非流式生成（供评测与兜底使用）。"""
    if not llm.live:
        raise LLMError("未配置模型 Key")
    started = time.perf_counter()
    text = ""
    async for delta in llm.chat_stream(
        answer_messages(cfg, question, focus, history, level, references, memory, prev_qa, mode_note),
        temperature=0.7,
        max_tokens=700,
        model=settings.answer_model,
        trace=trace,
        thinking=thinking,
        web_search=web_search,
        category=ANSWER_CATEGORY.get(level, "答案"),
    ):
        text += delta
    log.info(
        "非流式参考答案(%s版)完成 trace=%s 字数=%d 用时=%.1fs",
        level,
        trace or "-",
        len(text),
        time.perf_counter() - started,
    )
    return text


async def fix_placeholders(answer: str, question: str, trace: str = "") -> str:
    """把回答里残留的 XX / 【】 / 若干 占位符改成具体值；失败返回空串。"""
    if not llm.live or not answer.strip():
        return ""
    messages = [
        {"role": "system", "content": "你是面试话术校对，只输出改写后的回答正文。"},
        {
            "role": "user",
            "content": (
                "下面这段面试回答里有 XX、X、【】、N、若干 之类的占位符。请把它们换成具体数字或"
                "明确量级（优先用回答里已经出现的数字；没有就按上下文给一个合理的具体值/量级），"
                "其它内容不要改，已有的英文注音括号保留。只输出改写后的回答：\n\n"
                f"问题：{question}\n回答：{answer}"
            ),
        },
    ]
    text = ""
    async for delta in llm.chat_stream(
        messages,
        temperature=0.2,
        max_tokens=900,
        model=settings.answer_model,
        trace=trace,
        thinking=False,
        category="答案·校对",
    ):
        text += delta
    fixed = text.strip()
    if fixed:
        log.info("占位符已改写 trace=%s 原字数=%d 新字数=%d", trace or "-", len(answer), len(fixed))
    return fixed


# ---------------------------------------------------------------- 相关性


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text)
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def relevance(question: str, answer: str, keywords: list[str] | None = None) -> float:
    """答案与问题的相关度 0-1：关键词命中率与大字符重合度取较大值。

    只用于发现"八杆子打不着"的空答案，不做精细打分。
    """
    if not answer.strip():
        return 0.0
    keyword_hits = 0.0
    if keywords:
        hits = sum(1 for word in keywords if word and word in answer)
        keyword_hits = hits / len(keywords)

    q_grams = _bigrams(question)
    a_grams = _bigrams(answer)
    overlap = len(q_grams & a_grams) / max(1, len(q_grams))

    return max(keyword_hits, min(1.0, overlap * 2.5))
