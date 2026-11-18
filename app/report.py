"""面试结束后的评分：把逐轮问答变成可读的复盘报告。"""

from __future__ import annotations

import logging
import re

from . import profile, prompts
from .config import settings
from .llm import LLMError, llm
from .schemas import SessionConfig
from .judge import relevance

log = logging.getLogger("interview.report")

def _heuristic_round_score(question: str, user_answer: str, model_answer: str) -> int:
    text = user_answer.strip()
    if not text:
        return 25
    score = 45
    score += min(20, len(text) // 12)
    digits = len(re.findall(r"\d+", text))
    score += min(10, digits * 3)
    if any(word in text for word in ("首先", "其次", "最后", "第一", "第二", "因为", "所以")):
        score += 6
    if text in model_answer:
        score += 5
    if relevance(question, text) < 0.15:
        score -= 15
    return max(10, min(95, score))


def heuristic_report(cfg: SessionConfig, rounds: list[dict]) -> dict:
    scores = [_heuristic_round_score(item["question"], item.get("user_answer", ""), item.get("answer", "")) for item in rounds]
    overall = round(sum(scores) / len(scores)) if scores else 55

    offsets = {"问题理解": 5, "回答结构": -3, "技术深度": -6, "表达清晰": 2, "岗位匹配": 1}
    dimensions = [
        {"name": name, "score": max(20, min(96, overall + offset)), "comment": _comment(overall + offset)}
        for name, offset in offsets.items()
    ]
    per_question = [
        {"index": index + 1, "score": score, "comment": _round_comment(score)}
        for index, score in enumerate(scores)
    ]

    ordered = sorted(scores, reverse=True)
    best = ordered[0] if ordered else 60
    worst = ordered[-1] if ordered else 40
    return {
        "overall_score": overall,
        "verdict": "整体表现不错，个别题细节不足" if overall >= 72 else ("基础可以，但答题不够具体" if overall >= 58 else "目前还达不到岗位要求，建议系统准备"),
        "dimensions": dimensions,
        "chains": _heuristic_chains(rounds, scores),
        "per_question": per_question,
        "strengths": [f"最好的一题拿到 {best} 分", f"共完成 {len(rounds)} 轮问答"],
        "gaps": [f"最弱的一题只有 {worst} 分", "部分回答缺少量化结果", "追问细节时容易绕开核心"],
        "action_plan": [
            "把 2 个核心项目写成 STAR 卡片，各控在 90 秒内讲完",
            "每个项目补齐 3 个数字：规模、性能、业务收益",
            "针对最弱的题，写出参考答案并大声念三遍",
        ],
        "degraded": True,
    }


def _chain_lines(rounds: list[dict]) -> list[str]:
    """把问答按话题链分组渲染：根问题 + 追问/反问层级，便于模型先评链再评题。"""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for index, item in enumerate(rounds, start=1):
        key = str(item.get("root_id") or item.get("id") or index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, item))

    lines: list[str] = []
    for chain_no, key in enumerate(order, start=1):
        members = groups[key]
        root_question = members[0][1].get("question") or ""
        lines.append(f"=== 话题链 {chain_no}（根问题：{root_question}，共 {len(members)} 层）")
        for index, item in members:
            tag = "根"
            if item.get("challenge"):
                tag = "反问"
            elif item.get("followup"):
                tag = "追问"
            depth = item.get("depth") or 1
            lines.append(f"  第{index}轮（{tag} L{depth}）：{item.get('question', '')}")
            lines.append(f"    候选人回答：{item.get('user_answer') or '（没有作答）'}")
            lines.append(f"    参考答案：{item.get('answer') or '（未生成）'}")
        lines.append("")
    return lines


def _heuristic_chains(rounds: list[dict], scores: list[int]) -> list[dict]:
    """兜底：按链聚合单题分，给出链整体分和最虚的一层。"""
    groups: dict[str, list[tuple[int, dict, int]]] = {}
    order: list[str] = []
    for index, (item, score) in enumerate(zip(rounds, scores), start=1):
        key = str(item.get("root_id") or item.get("id") or index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, item, score))

    chains = []
    for key in order:
        members = groups[key]
        chain_scores = [score for _, _, score in members]
        avg = round(sum(chain_scores) / len(chain_scores))
        weakest_index, weakest_item, weakest_score = min(members, key=lambda row: row[2])
        depth = len(members)
        if depth > 1 and avg < 65:
            comment = f"追问了 {depth - 1} 层后回答明显变虚，链路后半段没兜住"
        elif depth > 1 and avg >= 78:
            comment = f"追问了 {depth - 1} 层仍能给出细节，链路稳定"
        elif depth > 1:
            comment = f"追问 {depth - 1} 层后细节一般，部分层答得空"
        else:
            comment = "单轮提问，按单题表现评分"
        chains.append(
            {
                "root_question": members[0][1].get("question") or "",
                "depth": depth,
                "score": avg,
                "comment": comment,
                "weakest_layer": weakest_index,
            }
        )
    return chains


def _comment(score: int) -> str:
    if score >= 80:
        return "表现优于同级候选人"
    if score >= 65:
        return "达到岗位基线，仍有提升空间"
    return "明显不足，需要专项训练"


def _round_comment(score: int) -> str:
    if score >= 80:
        return "回答到位，有细节"
    if score >= 60:
        return "基本答到点，但不够具体"
    return "答得偏空泛或跑题，建议按参考答案重说一遍"


async def build_report(cfg: SessionConfig, rounds: list[dict]) -> dict:
    if not rounds:
        return {
            "overall_score": 0,
            "verdict": "本次没有识别到有效问答，无法评分",
            "dimensions": [],
            "per_question": [],
            "strengths": [],
            "gaps": [],
            "action_plan": ["确认麦克风与系统声音都已采集到，再完整面一次"],
            "degraded": True,
        }

    if llm.live:
        try:
            lines = _chain_lines(rounds)
            data = await llm.chat_json(
                [
                    {"role": "system", "content": prompts.render("report.system")},
                    {
                        "role": "user",
                        "content": prompts.render(
                            "report.user",
                            role=cfg.role,
                            jd=cfg.background.strip()[:1500] or "未提供",
                            rounds="\n".join(lines),
                        ),
                    },
                ],
                temperature=0.3,
                max_tokens=2400 if settings.report_thinking else 1600,
                thinking=bool(settings.report_thinking),
                category="评分报告",
            )
            result = _normalize(data, len(rounds))
            if not result.get("chains"):
                scores = [
                    int(item.get("score") or 0)
                    for item in (result.get("per_question") or [])
                ]
                if len(scores) < len(rounds):
                    scores = [
                        _heuristic_round_score(
                            item["question"],
                            item.get("user_answer", ""),
                            item.get("answer", ""),
                        )
                        for item in rounds
                    ]
                result["chains"] = _heuristic_chains(rounds, scores[: len(rounds)])
                log.info("模型未返回话题链评分，已用启发式补齐 %d 条", len(result["chains"]))
            _remember(result)
            log.info(
                "评分报告已生成（模型）: 总分=%s 维度=%d 每题=%d 短板=%d",
                result.get("overall_score"),
                len(result.get("dimensions") or []),
                len(result.get("per_question") or []),
                len(result.get("gaps") or []),
            )
            return result
        except LLMError as exc:
            log.warning("报告生成失败，降级启发式: %s", exc)

    result = heuristic_report(cfg, rounds)
    log.info("评分报告已生成（启发式兜底）: 总分=%s", result.get("overall_score"))
    _remember(result)
    return result


def _remember(report: dict) -> None:
    """把这次的短板和亮点累积到跨场画像里。"""
    try:
        profile.remember("gap", [str(x) for x in (report.get("gaps") or [])])
        profile.remember("strength", [str(x) for x in (report.get("strengths") or [])])
    except Exception:  # noqa: BLE001 - 记忆失败不影响评分
        log.exception("写入长期画像失败")


def _normalize(data: dict, count: int) -> dict:
    def score(value: object) -> int:
        try:
            return max(0, min(100, int(float(value))))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    dimensions = [
        {"name": str(item.get("name")), "score": score(item.get("score")), "comment": str(item.get("comment") or "")}
        for item in (data.get("dimensions") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    per_question = [
        {
            "index": int(item.get("index") or index + 1),
            "score": score(item.get("score")),
            "comment": str(item.get("comment") or ""),
        }
        for index, item in enumerate(data.get("per_question") or [])
        if isinstance(item, dict)
    ]
    chains = []
    for item in data.get("chains") or []:
        if not isinstance(item, dict):
            continue
        try:
            weakest = int(item.get("weakest_layer") or 0)
        except (TypeError, ValueError):
            weakest = 0
        chains.append(
            {
                "root_question": str(item.get("root_question") or "").strip(),
                "depth": max(1, min(20, int(float(item.get("depth") or 1)))),
                "score": score(item.get("score")),
                "comment": str(item.get("comment") or "").strip(),
                "weakest_layer": weakest,
            }
        )
    return {
        "overall_score": score(data.get("overall_score")),
        "verdict": str(data.get("verdict") or "").strip(),
        "dimensions": dimensions,
        "chains": chains,
        "per_question": per_question[:count],
        "strengths": [str(x) for x in (data.get("strengths") or [])],
        "gaps": [str(x) for x in (data.get("gaps") or [])],
        "action_plan": [str(x) for x in (data.get("action_plan") or [])],
        "degraded": False,
    }
