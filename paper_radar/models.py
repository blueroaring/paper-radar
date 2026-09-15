"""领域模型：全部用 dataclass，方便在数据源 / 排序 / 摘要 / UI 之间传递。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any


# --------------------------------------------------------------------------- #
# 唯一标识
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize_title(title: str) -> str:
    """归一化标题，用于跨库去重（大小写、标点、空白差异都不该算两篇）。"""
    text = (title or "").strip().lower()
    text = _NON_WORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def fingerprint(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
) -> str:
    """生成稳定指纹：DOI > arXiv ID > 归一化标题。

    同一篇文章在 Google Scholar / arXiv / OpenAlex 里指纹一致，因此可以合并。
    """
    doi = (doi or "").strip().lower()
    if doi:
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
        return "doi:" + hashlib.sha1(doi.encode("utf-8")).hexdigest()[:16]
    aid = (arxiv_id or "").strip().lower()
    if aid:
        aid = re.sub(r"^arxiv:", "", aid)
        aid = re.sub(r"v\d+$", "", aid)
        return "arxiv:" + hashlib.sha1(aid.encode("utf-8")).hexdigest()[:16]
    return "title:" + hashlib.sha1(normalize_title(title or "").encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 论文
# --------------------------------------------------------------------------- #


@dataclass
class Paper:
    """一篇候选论文。字段取自各数据源的交集，其余进 extra。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""                 # 发表在哪里（会议/期刊名）
    venue_detail: str = ""          # 卷期页码等补充
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    abstract: str = ""
    citations: int | None = None
    item_type: str = "journalArticle"   # Zotero 条目类型建议
    source: str = ""                    # 首个发现它的数据源
    sources: list[str] = field(default_factory=list)  # 所有命中的数据源
    extra: dict[str, Any] = field(default_factory=dict)

    # 排序与推荐相关（由 rank.py / summarize.py 填充）
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    summary: str = ""                   # 内容概要
    highlights: list[str] = field(default_factory=list)  # 文章特色
    reason: str = ""                    # 推荐理由
    matched_keywords: list[str] = field(default_factory=list)

    # ----------------------------------------------------------------- #
    @property
    def key(self) -> str:
        return fingerprint(doi=self.doi, arxiv_id=self.arxiv_id, title=self.title)

    def merge(self, other: "Paper") -> "Paper":
        """把另一条记录的有用字段补进来（自身优先，缺失才补）。"""
        if other is self:
            return self
        for name in ("abstract", "venue", "venue_detail", "doi", "arxiv_id", "url", "item_type"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if self.year is None and other.year is not None:
            self.year = other.year
        if not self.authors and other.authors:
            self.authors = list(other.authors)
        if self.citations is None and other.citations is not None:
            self.citations = other.citations
        elif other.citations is not None and self.citations is not None:
            self.citations = max(self.citations, other.citations)
        for src in [other.source, *other.sources]:
            if src and src not in self.sources:
                self.sources.append(src)
        self.extra.update({k: v for k, v in other.extra.items() if k not in self.extra})
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d


# --------------------------------------------------------------------------- #
# 研究主题（检索方向）
# --------------------------------------------------------------------------- #


@dataclass
class Seed:
    """种子论文：用户可以直接给标题 / DOI / arXiv ID / URL。"""

    value: str = ""            # 用户原始输入
    title: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""


@dataclass
class Topic:
    """一个可检索的研究方向。direction 是自然语言概要，queries 是落地检索式。"""

    id: int | None = None
    name: str = ""
    direction: str = ""                       # 用户写的方向概要（中文/英文都行）
    seeds: list[Seed] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)          # 正向关键词
    negative_keywords: list[str] = field(default_factory=list)  # 排除词
    queries: list[str] = field(default_factory=list)            # 实际下发给各数据源的检索式
    filters: dict[str, Any] = field(default_factory=dict)       # 年份/引用/venue 白黑名单等
    zotero_collection: str = ""               # 推荐入库的默认分类
    enabled: bool = True
    profile_summary: str = ""                 # LLM 归纳后的方向描述（回填给用户确认）
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class Recommendation:
    """推荐表的一行：论文 + 三个说明列 + 相关度。"""

    paper: Paper
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = self.paper.to_dict()
        d.update(
            {
                "score": round(self.score, 4),
                "score_parts": {k: round(v, 4) for k, v in self.score_parts.items()},
                "summary": self.summary,
                "highlights": list(self.highlights),
                "reason": self.reason,
            }
        )
        return d
