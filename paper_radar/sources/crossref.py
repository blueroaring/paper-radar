"""Crossref 数据源（免费、无需 key、元数据权威）。

为什么默认启用它：dblp 在不少网络下会遇到反爬挑战页、Semantic Scholar 无 key 时几乎必然 429，
而 Crossref 提供**发表处、年份、DOI、作者、引用数**这些"推荐表"必需字段，且稳定可达。
它对计算机会议/期刊覆盖良好（IEEE/ACM/Elsevier/Springer 都注册了 DOI）。
"""

from __future__ import annotations

import re

from ..models import Paper
from .base import Source, year_in_range

_TAGS = re.compile(r"<[^>]+>")

# Crossref 的 type → Zotero 条目类型
TYPE_MAP = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "posted-content": "preprint",
    "report": "report",
    "dissertation": "thesis",
}
# 只接受这些类型，过滤掉 component（补充材料 PDF）、dataset 之类的噪声
ACCEPTED = set(TYPE_MAP)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", text or "")).strip()


def item_to_paper(item: dict) -> Paper | None:
    titles = item.get("title") or []
    title = _clean(titles[0]) if titles else ""
    if not title:
        return None
    containers = item.get("container-title") or item.get("short-container-title") or []
    venue = containers[0].strip() if containers else ""
    event = (item.get("event") or {}).get("name", "")
    if not venue and event:
        venue = event

    year = None
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    if parts and isinstance(parts[0], int):
        year = parts[0]

    authors = []
    for author in item.get("author") or []:
        name = " ".join(x for x in (author.get("given"), author.get("family")) if x).strip()
        if name:
            authors.append(name)

    doi = (item.get("DOI") or "").strip()
    kind = (item.get("type") or "").lower()
    return Paper(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        abstract=_clean(item.get("abstract") or ""),
        citations=item.get("is-referenced-by-count"),
        item_type=TYPE_MAP.get(kind, "journalArticle"),
        source="crossref",
        sources=["crossref"],
        extra={"crossref_type": kind, "publisher": item.get("publisher", "")},
    )


class CrossrefSource(Source):
    name = "crossref"
    label = "Crossref"
    homepage = "https://www.crossref.org"
    kinds = ("journal", "conference")

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoint = self.section.get("endpoint", "https://api.crossref.org/works")
        filters = [f"type:{t}" for t in (self.section.get("types") or sorted(ACCEPTED))]
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        params = {
            "query.bibliographic": query,
            "rows": max(1, min(limit * 2, 100)),
            "select": "title,DOI,issued,container-title,short-container-title,type,author,abstract,"
            "is-referenced-by-count,event,URL,publisher",
            "filter": ",".join(filters),
        }
        mailto = (self.section.get("mailto") or "").strip()
        if mailto:
            params["mailto"] = mailto
        data = self.http.get_json(endpoint, params=params)
        items = ((data or {}).get("message") or {}).get("items") or []
        papers: list[Paper] = []
        for item in items:
            if (item.get("type") or "").lower() not in ACCEPTED:
                continue
            paper = item_to_paper(item)
            if paper and year_in_range(paper.year, year_from, year_to):
                papers.append(paper)
            if len(papers) >= limit:
                break
        return papers
