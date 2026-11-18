"""意图识别评测：把复杂对话场景跑一遍意图识别器，量化准确率与答案相关性。

用法：
    python tools/eval_judge.py                 # 用当前配置（有 Key 走模型，无 Key 走启发式）
    python tools/eval_judge.py --answers       # 额外生成话术并检查相关性（耗 token，需 Key）
    python tools/eval_judge.py --verbose       # 打印每个用例的判定过程

产出：docs/eval-report.md 与 docs/eval-report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import embedding, judge, memory  # noqa: E402
from app.llm import llm  # noqa: E402
from app.schemas import SessionConfig  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ 这类符号会崩，统一转成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


SCENARIOS = ROOT / "tools" / "scenarios.jsonl"
OUT_MD = ROOT / "docs" / "eval-report.md"
OUT_JSON = ROOT / "docs" / "eval-report.json"

NEED_ANSWER = {"question", "instruction"}


def load_cases(path: Path) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def to_history(case: dict) -> list[dict]:
    """把用例转成对话历史；给每轮一个递增的时间戳，模拟真实面试的节奏。"""
    base = 1794900000.0
    history = []
    for index, turn in enumerate(case["turns"]):
        history.append(
            {
                "channel": turn["speaker"],
                "text": turn["text"],
                "ts": base + index * 9.0,
            }
        )
    return history


def semantic_sim(expected: str, actual: str) -> float:
    """用本地 bge 向量算语义相似度：表述不同但意思一致也能得高分。"""
    if not expected:
        return 1.0 if not actual else 0.0
    if not actual:
        return 0.0
    try:
        vectors = embedding.embed_cached([expected, actual])
        a, b = vectors.get(expected), vectors.get(actual)
        if a is None or b is None:
            return coverage(expected, actual)
        import numpy as np

        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, float(np.dot(a, b) / denom)))
    except Exception:  # noqa: BLE001 - 语义指标失败就退回字面覆盖
        return coverage(expected, actual)


def coverage(expected: str, actual: str) -> float:
    if not expected:
        return 1.0 if not actual else 0.0
    if not actual:
        return 0.0
    grams = {expected[i : i + 2] for i in range(max(1, len(expected) - 1))}
    hits = sum(1 for gram in grams if gram in actual)
    return hits / max(1, len(grams))


def keyword_hit(keywords: list[str], text: str) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for word in keywords if word and word in text)
    return hits / len(keywords)


async def run_case(case: dict, cfg: RealtimeConfig, with_answers: bool, verbose: bool) -> dict:
    history = to_history(case)
    expect = case["expect"]
    last_text = history[-1]["text"]
    verdict = await judge.classify(cfg, history, examples=memory.similar(last_text, 3))

    predicted = verdict["type"]
    expected = expect["type"]
    type_ok = predicted == expected

    verify_info = verdict.get("verify") or {}
    answered = predicted in NEED_ANSWER and verify_info.get("ok", True) is not False
    if not answered and expected not in NEED_ANSWER and predicted in NEED_ANSWER:
        # 复核把“本想回答的问题”撤销了，产品结果就是没回答 → 对 aside/statement 而言判定正确
        type_ok = True
    q_score = coverage(expect.get("question", ""), verdict["question"]) if (expected in NEED_ANSWER and answered) else 0.0
    q_semantic = (
        semantic_sim(expect.get("question", ""), verdict["question"])
        if (expected in NEED_ANSWER and answered)
        else (1.0 if expected not in NEED_ANSWER else 0.0)
    )
    answer_score = None
    answer_text = ""
    if with_answers and expected in NEED_ANSWER and verdict["type"] in NEED_ANSWER:
        try:
            answer_text = await judge.compose_answer(
                cfg, verdict["question"], verdict["focus"], history, "standard"
            )
            answer_score = keyword_hit(expect.get("answer_keywords", []), answer_text)
        except Exception as exc:  # noqa: BLE001
            answer_text = f"<生成失败: {exc}>"

    row = {
        "id": case["id"],
        "category": case["category"],
        "expected": expected,
        "predicted": predicted,
        "type_ok": type_ok,
        "confidence": verdict["confidence"],
        "spans": verdict["spans"],
        "reason": verdict.get("reason", ""),
        "expected_question": expect.get("question", ""),
        "predicted_question": verdict["question"],
        "question_score": round(q_score, 2),
        "question_semantic": round(q_semantic, 2),
        "answered": answered,
        "followup_pred": bool(verdict.get("followup")),
        "followup_expect": bool(expect.get("followup")),
        "challenge_pred": bool(verdict.get("challenge")),
        "challenge_expect": bool(expect.get("challenge")),
        "verify": verify_info,
        "answer_score": None if answer_score is None else round(answer_score, 2),
        "answer": answer_text[:200],
        "note": expect.get("note", ""),
    }

    if verbose:
        flag = "OK " if type_ok else "ERR"
        print(f"[{flag}] {case['id']} {case['category']} 期望={expected} 实得={predicted} 置信={verdict['confidence']:.2f}")
        print(f"       期望问题: {expect.get('question') or '-'}")
        print(f"       实得问题: {verdict['question'] or '-'}")
        if answer_score is not None:
            print(f"       答案关键词覆盖: {answer_score:.2f}")
    return row


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    correct = sum(1 for row in rows if row["type_ok"])
    # 产品上真正关心的是"该不该开口"：statement/chitchat 的边界本就模糊，
    # 单看类型准确率会低估效果，所以把"是否需要回答"单独列为第一指标。
    need_correct = sum(
        1
        for row in rows
        if (row["expected"] in NEED_ANSWER) == bool(row.get("answered", row["predicted"] in NEED_ANSWER))
    )
    fp = [row for row in rows if row["expected"] not in NEED_ANSWER and row["predicted"] in NEED_ANSWER]
    fn = [row for row in rows if row["expected"] in NEED_ANSWER and row["predicted"] not in NEED_ANSWER]
    q_rows = [row for row in rows if row["expected"] in NEED_ANSWER]
    rejected = [row for row in rows if (row.get("verify") or {}).get("ok") is False]
    follow_rows = [row for row in rows if row.get("followup_expect")]
    challenge_rows = [row for row in rows if row.get("challenge_expect")]
    aside_rows = [row for row in rows if row["expected"] == "aside"]

    return {
        "total": total,
        "correct": correct,
        "type_accuracy": round(correct / total, 4) if total else 0.0,
        "need_accuracy": round(need_correct / total, 4) if total else 0.0,
        "false_positive": len(fp),
        "false_positive_rate": round(len(fp) / total, 4) if total else 0.0,
        "false_negative": len(fn),
        "false_negative_rate": round(len(fn) / total, 4) if total else 0.0,
        "question_coverage_avg": round(sum(row["question_score"] for row in q_rows) / len(q_rows), 3) if q_rows else 0,
        "question_semantic_avg": round(sum(row["question_semantic"] for row in q_rows) / len(q_rows), 3) if q_rows else 0,
        "verify_rejected": len(rejected),
        "followup_accuracy": round(
            sum(1 for row in follow_rows if row.get("followup_pred") and row.get("answered"))
            / len(follow_rows), 3
        ) if follow_rows else None,
        "challenge_accuracy": round(
            sum(1 for row in challenge_rows if row.get("challenge_pred") and row.get("answered"))
            / len(challenge_rows), 3
        ) if challenge_rows else None,
        "aside_accuracy": round(
            sum(1 for row in aside_rows if row["predicted"] == "aside") / len(aside_rows), 3
        ) if aside_rows else None,
        "answer_keyword_avg": round(
            sum(row["answer_score"] for row in rows if row["answer_score"] is not None)
            / max(1, sum(1 for row in rows if row["answer_score"] is not None)),
            3,
        ),
    }


def write_report(rows: list[dict], summary: dict, mode: str, out_md: Path, out_json: Path) -> None:
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 意图识别评测报告",
        "",
        f"- 模式：{mode}",
        f"- 用例数：{summary['total']}",
        f"- **是否需要回答 准确率：{summary['need_accuracy'] * 100:.1f}%**（第一指标）",
        f"- 细分类型准确率：{summary['type_accuracy'] * 100:.1f}%",
        f"- 误报（不该出话术却出了）：{summary['false_positive']} 例 / {summary['false_positive_rate'] * 100:.1f}%",
        f"- 漏报（该出话术却没出）：{summary['false_negative']} 例 / {summary['false_negative_rate'] * 100:.1f}%",
        f"- 问题归纳覆盖率：{summary['question_coverage_avg'] * 100:.1f}%",
        f"- 问题语义相似度：{summary.get('question_semantic_avg', 0) * 100:.1f}%",
        f"- 接电话类判定准确率：{(summary['aside_accuracy'] or 0) * 100:.1f}%",
        f"- 答案关键词覆盖：{summary['answer_keyword_avg'] * 100:.1f}%（仅 --answers 模式有值）",
        "",
        "| 用例 | 分类 | 期望 | 实得 | 结论 | 归纳覆盖率 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['id']} | {row['category']} | {row['expected']} | {row['predicted']} | "
            f"{'✅' if row['type_ok'] else '❌'} | {row['question_score']:.2f} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_json.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", action="store_true", help="同时生成话术并检查相关性")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--file", default=str(SCENARIOS), help="用例文件，默认 tools/scenarios.jsonl")
    args = parser.parse_args()

    source = Path(args.file)
    cases = load_cases(source)
    if args.limit:
        cases = cases[: args.limit]

    cfg = SessionConfig(role="后端开发工程师", level="mid", answer_level="standard")
    mode = "模型意图识别" if llm.live else "启发式意图识别（未配置 LLM_API_KEY）"
    print(f"模式: {mode} | 用例: {len(cases)} | 经验库: {memory.stats()['total_feedback']} 条纠错")

    rows = []
    for case in cases:
        rows.append(await run_case(case, cfg, args.answers, args.verbose))

    summary = summarize(rows)
    suffix = "" if source == SCENARIOS else f"-{source.stem}"
    out_md = ROOT / "docs" / f"eval-report{suffix}.md"
    out_json = ROOT / "docs" / f"eval-report{suffix}.json"
    write_report(rows, summary, mode, out_md, out_json)

    print()
    print(f"是否需要回答 准确率: {summary['need_accuracy'] * 100:.1f}%")
    print(f"细分类型准确率: {summary['type_accuracy'] * 100:.1f}%  ({summary['correct']}/{summary['total']})")
    print(f"误报: {summary['false_positive']}  漏报: {summary['false_negative']}")
    print(f"问题归纳覆盖率: {summary['question_coverage_avg'] * 100:.1f}%")
    print(f"问题语义相似度: {summary.get('question_semantic_avg', 0) * 100:.1f}%")
    print(f"二次校验拦截: {summary.get('verify_rejected', 0)} 条")
    if summary.get("followup_accuracy") is not None:
        print(f"追问识别率: {summary['followup_accuracy'] * 100:.1f}%")
    if summary.get("challenge_accuracy") is not None:
        print(f"反问识别率: {summary['challenge_accuracy'] * 100:.1f}%")
    if summary["aside_accuracy"] is not None:
        print(f"接电话类准确率: {summary['aside_accuracy'] * 100:.1f}%")
    print(f"报告: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
