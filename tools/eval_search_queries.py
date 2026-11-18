"""检索改写评测：意图识别顺带输出的 search_queries 到底能不能提升题库召回。

对比三条检索路线（同一批口语化问法、同一批题库）：
  A. 原话直接检索
  B. 用意图识别还原后的 question 检索
  C. question + search_queries 多路检索

用法：uv run python tools/eval_search_queries.py
退出码：0 = 评测完成（不论指标高低）；1 = 环境/模型不可用。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

_TMP = Path(tempfile.mkdtemp(prefix="bank-eval-queries-"))
os.environ["DATA_DIR"] = str(_TMP)

from app import bank, judge  # noqa: E402
from app.llm import llm  # noqa: E402
from app.schemas import SessionConfig  # noqa: E402

# (口语化原话, 题库标准问法, 参考答案)
CASES = [
    ("你们系统里检索出来的东西是怎么排序的", "如何将召回结果进行排序优化？", "粗排后接 cross-encoder 精排，取 top5。"),
    ("怎么判断召回得准不准", "如何衡量检索质量？", "Recall@k、MRR、NDCG，加人工抽检。"),
    ("文档一般切多大合适", "如何进行文本分块优化？", "按语义边界 300-500 字，重叠 10-15%。"),
    ("稠密向量和关键词检索有啥不一样", "密集检索和稀疏检索的区别？", "稠密语义泛化，稀疏关键词精确，互补使用。"),
    ("模型老是瞎编怎么办", "如何减少 LLM 的幻觉问题？", "检索约束 + 要求引用 + 低温度 + 拒答。"),
    ("向量库怎么选型", "向量数据库对比与选型建议", "按规模、过滤需求、运维成本选。"),
    ("查询改写你们做过吗", "如何进行 Query 重写优化？", "多查询扩展 + HyDE，合并去重。"),
    ("RAG 和 SFT 哪个好", "RAG 与微调有什么区别？", "RAG 改输入可溯源，SFT 改参数塑风格。"),
    ("大模型答得好不好怎么评估", "如何构建 RAG 端到端性能评估体系？", "检索层、生成层、端到端分层评估。"),
    ("重排模型怎么用", "结合 Rerank 的 RAG 优化方案", "召回 top50 交 reranker，取 top5。"),
    ("你们线上跑的是哪个模型", "你们线上用的是哪个大模型？", "主力模型生成，小模型意图识别。"),
    ("多模态的检索怎么搞", "如何构建多模态数据检索模块？", "文本图像统一向量空间联合检索。"),
]


def seed() -> None:
    for _, question, answer in CASES:
        bank.add("评测", question, answer)


def score(name: str, searcher, expected_by_text: dict[str, int], limit: int = 3) -> dict:
    hits1 = hits3 = 0
    mrr = 0.0
    misses: list[tuple[str, str, list[str]]] = []
    for raw, question, _ in CASES:
        expected = expected_by_text.get(question.strip())
        got = searcher(raw, question)
        positions = [index + 1 for index, item in enumerate(got) if item["id"] == expected]
        if positions:
            pos = positions[0]
            mrr += 1.0 / pos
            hits1 += 1 if pos == 1 else 0
            hits3 += 1 if pos <= limit else 0
        else:
            misses.append((raw, question, [item["question"][:20] for item in got]))
    total = len(CASES)
    return {
        "name": name,
        "recall@1": hits1 / total,
        "recall@3": hits3 / total,
        "mrr": mrr / total,
        "misses": misses,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="检索改写评测")
    parser.add_argument("--repeat", type=int, default=2, help="重复次数（模型有随机性，取平均）")
    args = parser.parse_args()

    if not llm.live:
        print("未配置大模型 Key，无法评测检索改写")
        return 1

    seed()
    items = bank.list_items()
    by_question = {item["question"].strip(): item["id"] for item in items}
    cfg = SessionConfig(role="RAG 算法工程师")

    collected: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    last_analysis: dict[str, tuple[str, list[str]]] = {}
    with_queries = 0

    for run in range(max(1, args.repeat)):
        analyzed: list[tuple[str, str, list[str]]] = []
        for raw, _, _ in CASES:
            history = [{"channel": "interviewer", "text": raw, "ts": 0.0}]
            verdict = await judge.classify(cfg, history)
            question = str(verdict.get("question") or "").strip()
            queries = [str(item).strip() for item in (verdict.get("search_queries") or [])]
            queries = [item for item in queries if item and item != question]
            analyzed.append((raw, question or raw, queries[:3]))
        with_queries += sum(1 for _, _, queries in analyzed if queries)

        analysis = {raw: (question, queries) for raw, question, queries in analyzed}
        last_analysis = analysis
        if run == 0:
            print("—— 第 1 轮还原结果（示例）——")
            for raw, question, queries in analyzed:
                print(f"原话「{raw}」→ 问题「{question}」 检索改写 {queries}")

        collected["A"].append(
            score(
                "A. 原话直接检索（不开意图识别改写）",
                lambda raw, _question: bank.search(raw, 3),
                by_question,
            )
        )
        collected["B"].append(
            score(
                "B. 意图识别还原后的 question 检索",
                lambda raw, _question: bank.search(analysis[raw][0], 3),
                by_question,
            )
        )

        def multi(raw: str, _question: str, _analysis=analysis) -> list[dict]:
            question, queries = _analysis[raw]
            return bank.search_multi([question, *queries], 3)

        collected["C"].append(
            score("C. question + search_queries 多路检索", multi, by_question)
        )

    def average(key: str) -> dict:
        rows = collected[key]
        total = len(rows)
        return {
            "recall@1": sum(row["recall@1"] for row in rows) / total,
            "recall@3": sum(row["recall@3"] for row in rows) / total,
            "mrr": sum(row["mrr"] for row in rows) / total,
            "misses": rows[-1]["misses"],
        }

    a, b, c = average("A"), average("B"), average("C")
    labels = [
        ("A. 原话直接检索（不开意图识别改写）", a),
        ("B. 意图识别还原后的 question 检索", b),
        ("C. question + search_queries 多路检索", c),
    ]
    for name, result in labels:
        print("=" * 64)
        print(name)
        print(
            f"  Recall@1 {result['recall@1']:.1%}   "
            f"Recall@3 {result['recall@3']:.1%}   MRR {result['mrr']:.3f}"
        )
    print("\n" + "=" * 64)
    print(f"平均轮数：{max(1, args.repeat)}")
    print(
        f"提升：A→B Recall@1 {a['recall@1']:.0%}→{b['recall@1']:.0%}，"
        f"B→C Recall@1 {b['recall@1']:.0%}→{c['recall@1']:.0%} | "
        f"Recall@3 {b['recall@3']:.0%}→{c['recall@3']:.0%} | "
        f"MRR {b['mrr']:.2f}→{c['mrr']:.2f}"
    )
    print(f"生成检索改写的用例：{with_queries}/{len(CASES) * max(1, args.repeat)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
