"""ASR 转写后处理：修正英文技术词的常见误识别。

火山豆包流式 2.0 对中文很稳，但英文缩写（尤其 RAG / LLM / Faiss）容易听成
red / LM / face 这类词。这里做一层保守的词级纠正，只处理面试场景里几乎不会
有其它含义的写法；真正的第一道防线是识别请求里的领域热词（见 volc_speech.py）。
"""

from __future__ import annotations

import re

# 顺序有意义：先处理带空格的短语，再处理单词
_RULES: list[tuple[re.Pattern[str], str]] = [
    # LM agent / L M agent / Lm agent -> LLM agent
    (re.compile(r"(?i)(?<![A-Za-z])L\s*M\s+agent(?![A-Za-z])"), "LLM agent"),
    # LM / Lm / lm 单独出现时按 LLM 处理
    (re.compile(r"(?i)(?<![A-Za-z])L\s*M(?![A-Za-z])"), "LLM"),
    # RAG 常见误识别：red / rag
    (re.compile(r"(?i)(?<![A-Za-z])red(?![A-Za-z])"), "RAG"),
    (re.compile(r"(?i)(?<![A-Za-z])rag(?![A-Za-z])"), "RAG"),
    # 仿真里 "LLM agent" 被听成 alpine，后面通常跟逗号 + 我/这/它
    (re.compile(r"(?i)(?<![A-Za-z])alpine(?=\s*[,，]\s*(?:我|这|那|它|他))"), "LLM agent"),
    # Milvus 的常见写法
    (re.compile(r"(?i)(?<![A-Za-z])milas(?![A-Za-z])"), "Milvus"),
    (re.compile(r"(?i)(?<![A-Za-z])milvus(?![A-Za-z])"), "Milvus"),
    # Faiss 常见误识别（face/faith/fess 单独出现时多半是 Faiss 的听写）
    (re.compile(r"(?i)(?<![A-Za-z])faiss(?![A-Za-z])"), "Faiss"),
    (re.compile(r"(?i)(?<![A-Za-z])face(?![A-Za-z])"), "Faiss"),
    (re.compile(r"(?i)(?<![A-Za-z])faith(?![A-Za-z])"), "Faiss"),
    (re.compile(r"(?i)(?<![A-Za-z])fess(?![A-Za-z])"), "Faiss"),
]


def correct_transcript(text: str) -> str:
    """对转写做保守纠错；没有命中就原样返回。"""
    if not text:
        return text
    result = text
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)
    return result
