"""arXiv 数据源。

两条通道，互为备份：
  1. **官方 Atom API**（可查询、可排序）—— 首选；但在部分网络下会被 429 限流或超时；
  2. **分类 RSS**（https://rss.arxiv.org/rss/cs.NI）—— API 不可用时自动降级，
     它只包含最新一次公告的论文，但对"每日最新"这个场景恰好最合适，
     本地按检索词过滤后仍能拿到标题/摘要/作者/DOI。

因此即使 API 挂了，arXiv 这条线也不会整段失效。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..models import Paper
from .base import Source, year_in_range

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
RSS_ARXIV = "{http://arxiv.org/schemas/atom}"

# 已经带字段前缀的检索式直接透传，避免把 ti:foo 包成 all:(ti:foo)
_FIELD_RE = re.compile(r"\b(all|ti|abs|au|cat|co|jr|rn|id|doi):", re.I)


def parse_arxiv_feed(xml_text: str) -> list[Paper]:
    """把 arXiv Atom 响应解析成 Paper 列表（独立函数，便于离线测试）。"""
    papers: list[Paper] = []
    if not xml_text or "<entry" not in xml_text:
        return papers
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return papers
    for entry in root.findall(f"{ATOM}entry"):
        title = _clean(_text(entry.find(f"{ATOM}title")))
        if not title:
            continue
        raw_id = _text(entry.find(f"{ATOM}id"))
        arxiv_id = ""
        match = re.search(r"/abs/(.+)$", raw_id or "")
        if match:
            arxiv_id = match.group(1)
        abstract = _clean(_text(entry.find(f"{ATOM}summary")))
        published = _text(entry.find(f"{ATOM}published")) or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        authors = [
            _clean(_text(a.find(f"{ATOM}name")))
            for a in entry.findall(f"{ATOM}author")
        ]
        url = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("rel") == "alternate":
                url = link.get("href") or ""
                break
        primary = entry.find(f"{ARXIV}primary_category")
        category = primary.get("term") if primary is not None else ""
        doi = _text(entry.find(f"{ARXIV}doi")) or ""
        comment = _clean(_text(entry.find(f"{ARXIV}comment"))) if entry.find(f"{ARXIV}comment") is not None else ""

        paper = Paper(
            title=title,
            authors=[a for a in authors if a],
            year=year,
            venue=f"arXiv preprint ({category})" if category else "arXiv preprint",
            doi=doi,
            arxiv_id=arxiv_id,
            url=url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
            abstract=abstract,
            item_type="preprint",
            source="arxiv",
            sources=["arxiv"],
            extra={"primary_category": category, "comment": comment, "published": published},
        )
        papers.append(paper)
    return papers


def _text(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_arxiv_rss(xml_text: str) -> list[Paper]:
    """解析 arXiv 分类 RSS（独立函数，便于离线测试）。"""
    papers: list[Paper] = []
    if not xml_text or "<item>" not in xml_text:
        return papers
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return papers
    for item in root.iter("item"):
        title = _clean(_text(item.find("title")))
        link = _clean(_text(item.find("link")))
        if not title or not link:
            continue
        description = _text(item.find("description")) or ""
        announce = ""
        abstract = description
        # description 形如： "arXiv:2609.13795v1 Announce Type: new \nAbstract: ..."
        head, _, tail = description.partition("Abstract:")
        if tail:
            abstract = tail
            match = re.search(r"Announce Type:\s*(\w+)", head)
            announce = match.group(1) if match else ""
        arxiv_id = ""
        match = re.search(r"/abs/([\w.\-/]+)$", link)
        if match:
            arxiv_id = match.group(1)
        if not arxiv_id:
            guid = _clean(_text(item.find("guid")))
            match = re.search(r"arXiv\.org:([\w.\-]+)", guid)
            if match:
                arxiv_id = match.group(1)

        creators = _clean(_text(item.find(f"{DC}creator")))
        authors = [a.strip() for a in creators.split(",") if a.strip()]
        category = _clean(_text(item.find("category")))
        pub_date = _clean(_text(item.find("pubDate")))
        doi = _clean(_text(item.find(f"{RSS_ARXIV}DOI")))
        year = None
        year_match = re.search(r"\b(19|20)\d{2}\b", pub_date)
        if year_match:
            year = int(year_match.group(0))

        papers.append(
            Paper(
                title=title,
                authors=authors,
                year=year,
                venue=f"arXiv preprint ({category})" if category else "arXiv preprint",
                doi=doi,
                arxiv_id=arxiv_id,
                url=link,
                abstract=_clean(abstract),
                item_type="preprint",
                source="arxiv",
                sources=["arxiv"],
                extra={
                    "primary_category": category,
                    "published": pub_date,
                    "announce_type": announce,
                    "channel": "rss",
                },
            )
        )
    return papers


def _matches_locally(paper: Paper, query: str) -> bool:
    """RSS 不支持检索式，只能在本地做一次宽松的关键词过滤，避免把整批公告都塞进推荐表。"""
    text = f"{paper.title} {paper.abstract}".lower()
    terms = [t for t in re.split(r"\s+(?:AND|OR)\s+|\s+", query.replace('"', " ")) if len(t) > 2]
    terms = [re.sub(r"[^a-z0-9\-]", "", t.lower()) for t in terms]
    terms = [t for t in terms if t]
    if not terms:
        return True
    hits = sum(1 for t in terms if t in text)
    # 至少命中 40% 的词，且不少于 2 个
    return hits >= max(2, int(len(terms) * 0.4))


class ArxivSource(Source):
    name = "arxiv"
    label = "arXiv"
    homepage = "https://arxiv.org"
    kinds = ("preprint",)

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoints = [self.section.get("endpoint", "https://export.arxiv.org/api/query")]
        endpoints += [
            e for e in self.section.get("fallback_endpoints", []) if e not in endpoints
        ]
        expr = self._build_query(query)
        params = {
            "search_query": expr,
            "start": 0,
            "max_results": max(1, min(limit * 2, 100)),
            "sortBy": self.section.get("sort_by", "relevance"),
            "sortOrder": "descending",
        }
        last_error: Exception | None = None
        for endpoint in endpoints:
            try:
                xml_text = self.http.get_text(endpoint, params=params)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            papers = parse_arxiv_feed(xml_text)
            if papers:
                return [p for p in papers if year_in_range(p.year, year_from, year_to)][:limit]

        if self.section.get("rss_fallback", True):
            papers = self._search_rss(query, limit=limit, year_from=year_from, year_to=year_to)
            if papers:
                return papers
        if last_error:
            raise last_error
        return []

    # ------------------------------------------------------------------ #
    def _search_rss(self, query: str, *, limit: int, year_from=None, year_to=None) -> list[Paper]:
        """分类 RSS 兜底：拉取配置里的 cs.* 分类最新公告，在本地按检索词过滤。"""
        template = self.section.get("rss_url_template", "https://rss.arxiv.org/rss/{category}")
        categories = list(self.section.get("categories", []) or [])
        if not categories:
            return []
        collected: list[Paper] = []
        errors: list[str] = []
        for category in categories[:4]:  # RSS 只用来兜底，别把时间都花在这
            url = template.format(category=category)
            try:
                xml_text = self.http.get_text(url, accept="application/rss+xml")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{category}: {exc}")
                continue
            collected.extend(parse_arxiv_rss(xml_text))
        filtered = [
            p for p in collected if _matches_locally(p, query) and year_in_range(p.year, year_from, year_to)
        ]
        if not filtered:
            if errors and not collected:
                raise RuntimeError("arXiv API 与 RSS 均失败：" + "; ".join(errors[:2]))
            return []
        return filtered[:limit]

    # ------------------------------------------------------------------ #
    def _build_query(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            return "all:computer science"
        if not _FIELD_RE.search(q):
            q = f"all:({q})"
        categories = list(self.section.get("categories", []) or [])
        if categories:
            cats = " OR ".join(f"cat:{c}" for c in categories)
            q = f"({q}) AND ({cats})"
        return q
