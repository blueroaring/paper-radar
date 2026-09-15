"""文本工具：分词、关键词抽取、句子切分、LLM 输出解析。

关键词抽取是"没有大模型也能用"的保底路径（对应配置 llm.provider = "none"）。
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

# 学术文本里的高频噪声词（可被 config 的 profile.stopwords 覆盖/追加）
DEFAULT_STOPWORDS = [
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "by", "at", "from",
    "is", "are", "was", "were", "be", "been", "being", "as", "that", "this", "these", "those",
    "it", "its", "we", "our", "you", "your", "they", "their", "he", "she", "his", "her",
    "can", "could", "may", "might", "will", "would", "shall", "should", "must", "do", "does",
    "did", "not", "no", "nor", "but", "if", "then", "than", "so", "such", "via", "using",
    "used", "use", "uses", "based", "toward", "towards", "novel", "new", "approach",
    "approaches", "method", "methods", "paper", "papers", "study", "studies", "results",
    "result", "show", "shows", "shown", "propose", "proposed", "present", "presents",
    "however", "moreover", "also", "which", "when", "where", "while", "how", "what", "who",
    "there", "here", "under", "over", "into", "between", "among", "across", "through",
    "each", "both", "all", "any", "some", "most", "many", "much", "very", "well", "more",
    "less", "high", "low", "large", "small", "first", "second", "third", "one", "two",
    "three", "et", "al", "ieee", "acm", "doi", "abstract", "introduction", "conclusion",
    "keywords", "figure", "table", "section", "appendix", "references", "https", "http",
    "www", "com", "org", "available", "online", "copyright", "author", "authors",
]


def tokenize(text: str) -> list[str]:
    """英文词 + 中文连续片段。保留连字符（buffer-management 视作一个 token）。"""
    if not text:
        return []
    lowered = text.lower()
    tokens = _TOKEN_RE.findall(lowered)
    tokens.extend(_CJK_RE.findall(text))
    return tokens


def keyword_counts(text: str, stopwords: set[str]) -> Counter:
    return Counter(t for t in tokenize(text) if t not in stopwords and len(t) > 2)


def extract_keywords(
    texts: list[str],
    *,
    stopwords: set[str] | None = None,
    top_k: int = 18,
    weights: list[float] | None = None,
) -> list[str]:
    """从若干文本里抽关键词（加权词频 + 二元短语）。"""
    stop = set(DEFAULT_STOPWORDS) | {s.lower() for s in (stopwords or set())}
    counter: Counter = Counter()
    bigrams: Counter = Counter()
    for idx, text in enumerate(texts):
        weight = weights[idx] if weights and idx < len(weights) else 1.0
        tokens = [t for t in tokenize(text) if t not in stop and len(t) > 2]
        counter.update({t: weight for t in tokens})
        for a, b in zip(tokens, tokens[1:]):
            bigrams[f"{a} {b}"] += weight * 1.5

    scored = {term: float(count) for term, count in counter.items()}
    for phrase, count in bigrams.items():
        if count >= 2.0:  # 只保留反复出现的短语，避免噪声
            scored[phrase] = scored.get(phrase, 0.0) + float(count)

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[str] = []
    for term, _ in ranked:
        if any(term in kept or kept in term for kept in out):
            continue
        out.append(term)
        if len(out) >= top_k:
            break
    return out


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def truncate(text: str, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;: ") + "…"


def slugify(text: str, fallback: str = "topic") -> str:
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:48] or fallback


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def extract_json_block(text: str) -> Any:
    """LLM 经常在 JSON 外包一层 ```json 或解释文字，这里做鲁棒提取。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None
