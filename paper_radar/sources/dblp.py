"""dblp 数据源（计算机领域专用书目库）。

优势：**天然只含计算机方向**，且会议/期刊名规范（SIGCOMM / NSDI / IEEE Trans. ...），
非常适合做"发表在哪里"这一列以及 venue 分区打分；缺点是没有摘要，需要靠别的源补。
"""

from __future__ import annotations

from ..models import Paper
from .base import Source, year_in_range


def hit_to_paper(hit: dict) -> Paper | None:
    info = hit.get("info") or {}
    title = (info.get("title") or "").strip().rstrip(".")
    if not title:
        return None
    authors_field = (info.get("authors") or {}).get("author") or []
    if isinstance(authors_field, dict):
        authors_field = [authors_field]
    authors = []
    for a in authors_field:
        name = a.get("text") if isinstance(a, dict) else str(a)
        if name:
            authors.append(name.strip())
    year_raw = info.get("year")
    year = int(year_raw) if str(year_raw).isdigit() else None
    kind = (info.get("type") or "").lower()
    if "conference" in kind or "workshop" in kind:
        item_type = "conferencePaper"
    elif "journal" in kind:
        item_type = "journalArticle"
    else:
        item_type = "journalArticle"
    ee = info.get("ee") or ""
    url = info.get("url") or ""
    return Paper(
        title=title,
        authors=authors,
        year=year,
        venue=info.get("venue") or "",
        venue_detail=info.get("volume") or "",
        doi=info.get("doi") or "",
        url=ee or url,
        abstract="",
        item_type=item_type,
        source="dblp",
        sources=["dblp"],
        extra={"dblp_key": info.get("key", ""), "dblp_type": info.get("type", "")},
    )


class DblpSource(Source):
    name = "dblp"
    label = "dblp"
    homepage = "https://dblp.org"
    kinds = ("journal", "conference")

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoint = self.section.get("endpoint", "https://dblp.org/search/publ/api")
        params = {
            "q": query,
            "format": self.section.get("format", "json"),
            "h": max(1, min(limit * 2, 100)),
        }
        data = self.http.get_json(endpoint, params=params)
        hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
        papers: list[Paper] = []
        for hit in hits:
            paper = hit_to_paper(hit)
            if paper and year_in_range(paper.year, year_from, year_to):
                papers.append(paper)
            if len(papers) >= limit:
                break
        return papers
