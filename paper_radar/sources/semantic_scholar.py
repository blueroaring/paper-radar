"""Semantic Scholar Graph API 数据源。

免费额度下并发很紧（无 key 时经常 429），但字段质量高（引用数、venue、领域）。
config 里可选填 api_key 提升限额；没 key 也能用，只是更容易被限流。
"""

from __future__ import annotations

from ..models import Paper
from .base import Source, year_in_range


class SemanticScholarSource(Source):
    name = "semantic_scholar"
    label = "Semantic Scholar"
    homepage = "https://www.semanticscholar.org"
    kinds = ("journal", "conference", "preprint")
    needs_key = False

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoint = self.section.get(
            "endpoint", "https://api.semanticscholar.org/graph/v1/paper/search"
        )
        params = {
            "query": query,
            "limit": max(1, min(limit, 100)),
            "fields": self.section.get(
                "fields",
                "title,abstract,year,venue,publicationVenue,externalIds,url,citationCount,"
                "authors,publicationTypes,fieldsOfStudy",
            ),
        }
        if year_from or year_to:
            params["year"] = f"{year_from or ''}-{year_to or ''}"
        fos = self.section.get("fields_of_study", "Computer Science")
        if fos:
            params["fieldsOfStudy"] = fos

        headers = {}
        api_key = (self.section.get("api_key") or "").strip()
        if api_key:
            headers["x-api-key"] = api_key

        def _fetch(extra: dict) -> list[dict]:
            p = dict(params)
            p.update(extra)
            data = self.http.get_json(endpoint, params=p, headers=headers)
            return (data or {}).get("data") or []

        results = _fetch({})
        if not results and fos:
            results = _fetch({"fieldsOfStudy": ""})

        papers: list[Paper] = []
        for item in results:
            paper = _to_paper(item)
            if paper and year_in_range(paper.year, year_from, year_to):
                papers.append(paper)
        return papers[:limit]

    def is_configured(self) -> bool:
        return True


def _to_paper(item: dict) -> Paper | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None
    ext = item.get("externalIds") or {}
    pub_venue = item.get("publicationVenue") or {}
    venue = item.get("venue") or pub_venue.get("name") or ""
    ptype = (item.get("publicationTypes") or [""])[0]
    if ptype == "JournalArticle":
        item_type = "journalArticle"
    elif ptype in ("Conference", "Review"):
        item_type = "conferencePaper"
    else:
        item_type = "preprint" if ext.get("ArXiv") else "journalArticle"
    return Paper(
        title=title,
        authors=[(a.get("name") or "") for a in (item.get("authors") or []) if a.get("name")],
        year=item.get("year"),
        venue=venue,
        doi=ext.get("DOI") or "",
        arxiv_id=ext.get("ArXiv") or "",
        url=item.get("url") or "",
        abstract=item.get("abstract") or "",
        citations=item.get("citationCount"),
        item_type=item_type,
        source="semantic_scholar",
        sources=["semantic_scholar"],
        extra={"fields": item.get("fieldsOfStudy") or [], "publication_types": item.get("publicationTypes") or []},
    )
