"""题库召回评测：对比「字符相似度」与「BM25 + 字符相似度混合」。

用口语化的改写问法去查接近真实题库的标准问法，看能不能召回对的那道题。

用法：python tools/eval_retrieval.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="bank-eval-"))
os.environ["DATA_DIR"] = str(_TMP)

from app import bank  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ 这类符号会崩，统一转成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


# (口语化查询, 题库里的标准问法, 参考答案)
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


def evaluate(searcher, limit: int = 3) -> dict:
    items = bank.list_items()
    by_question = {item["question"].strip(): item["id"] for item in items}

    hits1 = hits3 = 0
    mrr = 0.0
    misses: list[tuple[str, str, list[str]]] = []

    for query, question, _ in CASES:
        expected = by_question.get(question.strip())
        got = searcher(query, limit)
        positions = [index + 1 for index, item in enumerate(got) if item["id"] == expected]
        if positions:
            pos = positions[0]
            mrr += 1.0 / pos
            hits1 += 1 if pos == 1 else 0
            hits3 += 1 if pos <= 3 else 0
        else:
            misses.append((query, question, [item["question"][:20] for item in got]))

    total = len(CASES)
    return {
        "recall@1": hits1 / total,
        "recall@3": hits3 / total,
        "mrr": mrr / total,
        "misses": misses,
    }


def report(name: str, result: dict) -> None:
    print("=" * 64)
    print(name)
    print(
        f"  Recall@1 {result['recall@1']:.1%}   "
        f"Recall@3 {result['recall@3']:.1%}   MRR {result['mrr']:.3f}"
    )
    for query, question, got in result["misses"]:
        print(f"  ✗ 「{query}」")
        print(f"      期望：{question}")
        print(f"      实得：{got}")


def main() -> int:
    seed()
    print(f"题库条数: {len(bank.list_items())}\n")

    old = evaluate(bank.search_lexical)
    report("A. 旧方案：仅字符相似度", old)

    new = evaluate(bank.search)
    print()
    report("B. 新方案：BM25 + 字符相似度混合", new)

    print("\n" + "=" * 64)
    print(
        f"提升：Recall@1 {old['recall@1']:.0%} → {new['recall@1']:.0%}"
        f" | Recall@3 {old['recall@3']:.0%} → {new['recall@3']:.0%}"
        f" | MRR {old['mrr']:.2f} → {new['mrr']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
