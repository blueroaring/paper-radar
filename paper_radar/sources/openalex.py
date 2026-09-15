"""OpenAlex 数据源（免费、无需 key、覆盖面最广的开放学术图谱）。

优势：能给出**发表处（venue）**、年份、引用数、作者、摘要（倒排索引形式）——
正好补齐 Google Scholar 抓取不稳、arXiv 没有 venue/引用的短板，也用于把种子论文解析成元数据。
"""

from __future__ import annotations

from ..models import Paper
from .base import Source, year_in_range


def rebuild_abstract(inverted: dict | None) -> str:
    """OpenAlex 的摘要是 {词: [位置...]} 倒排索引，这里还原成正常文本。"""
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs or []:
            positions[i] = word
    return " ".join(positions[k] for k in sorted(positions))


def openalex_type_to_zotero(kind: str) -> str:
    kind = (kind or "").lower()
    if kind in ("preprint", "posted-content"):
        return "preprint"
    if "proceedings" in kind or kind in ("conference", "book-chapter"):
        return "conferencePaper"
    return "journalArticle"


def work_to_paper(work: dict) -> Paper | None:
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None
    doi = work.get("doi") or ""
    ids = work.get("ids") or {}
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in work.get("authorships") or []
    ]
    authors = [a for a in authors if a]

    primary = work.get("primary_location") or {}
    source_info = primary.get("source") or {}
    venue = source_info.get("display_name") or ""
    venue_detail = " ".join(
        str(x) for x in [source_info.get("issn_l"), work.get("biblio", {}).get("volume"),
                         work.get("biblio", {}).get("issue"), work.get("biblio", {}).get("first_page")]
        if x
    )
    url = primary.get("landing_page_url") or work.get("id") or ""
    year = work.get("publication_year")
    topic = (work.get("primary_topic") or {}).get("display_name") or ""

    return Paper(
        title=title,
        authors=authors,
        year=int(year) if year else None,
        venue=venue,
        venue_detail=venue_detail,
        doi=doi.replace("https://doi.org/", ""),
        url=url,
        abstract=rebuild_abstract(work.get("abstract_inverted_index")),
        citations=work.get("cited_by_count"),
        item_type=openalex_type_to_zotero(work.get("type", "")),
        source="openalex",
        sources=["openalex"],
        extra={
            "openalex_id": work.get("id", ""),
            "type": work.get("type", ""),
            "topic": topic,
            "open_access": (work.get("open_access") or {}).get("is_oa"),
            "arxiv_id": (ids.get("arxiv") or "") if isinstance(ids, dict) else "",
        },
    )


class OpenAlexSource(Source):
    name = "openalex"
    label = "OpenAlex"
    homepage = "https://openalex.org"
    kinds = ("journal", "conference", "preprint")

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoint = self.section.get("endpoint", "https://api.openalex.org/works")
        params = {
            "search": query,
            "per-page": max(1, min(limit * 2, 100)),
            "sort": "relevance_score:desc",
        }
        mailto = (self.section.get("mailto") or "").strip()
        if mailto:
            params["mailto"] = mailto

        filters = []
        if self.section.get("cs_only", True):
            concept = self.section.get("cs_concept_id", "C41008148")
            if concept:
                filters.append(f"concepts.id:{concept}")
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")

        def _fetch(extra_filters: list[str]) -> list[dict]:
            p = dict(params)
            if extra_filters:
                p["filter"] = ",".join(extra_filters)
            data = self.http.get_json(endpoint, params=p)
            return (data or {}).get("results") or []

        results = _fetch(filters)
        if not results and filters:
            # CS 概念过滤可能过窄（新论文尚未归类），退化为不限定学科
            results = _fetch([f for f in filters if not f.startswith("concepts.id:")])

        papers: list[Paper] = []
        for work in results:
            paper = work_to_paper(work)
            if paper and year_in_range(paper.year, year_from, year_to):
                papers.append(paper)
            if len(papers) >= limit:
                break
        return papers
