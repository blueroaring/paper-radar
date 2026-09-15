"""Google Scholar 数据源（页面解析，无官方 API）。

工程注意：
  * 必须带真实浏览器 UA，否则直接被拦；
  * 频率必须低（config 里默认 5s/次），命中 429 时 net.Fetcher 会退避重试；
  * 解析逻辑写成独立函数 parse_scholar_html()，方便用固定 HTML 做离线测试；
  * 抓不到时**只影响本数据源**，其它源照常出结果。
"""

from __future__ import annotations

import html as html_mod
import re
from urllib.parse import quote

from ..models import Paper
from .base import Source, year_in_range

_BLOCK_START = re.compile(r'<div[^>]*class="[^"]*\bgs_r\b[^"]*"')
_H3 = re.compile(r'<h3[^>]*class="[^"]*gs_rt[^"]*"[^>]*>(.*?)</h3>', re.S)
_LINK = re.compile(r'<a[^>]+href="([^"]+)"', re.I)
_GS_A = re.compile(r'<div[^>]*class="[^"]*gs_a[^"]*"[^>]*>(.*?)</div>', re.S)
_GS_RS = re.compile(r'<div[^>]*class="[^"]*gs_rs[^"]*"[^>]*>(.*?)</div>', re.S)
_CITED = re.compile(r"(?:Cited by|被引用次数：|被引用次数:)\s*([\d,]+)", re.I)
_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_TAGS = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    text = _TAGS.sub(" ", fragment or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def split_result_blocks(page: str) -> list[str]:
    """Scholar 的每条结果都是 <div class="gs_r ...">，按起点切片即可，不必引入 HTML 解析库。"""
    starts = [m.start() for m in _BLOCK_START.finditer(page or "")]
    blocks = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(page)
        blocks.append(page[start:end])
    return blocks


def parse_scholar_html(page: str, *, hl: str = "en") -> list[Paper]:
    papers: list[Paper] = []
    for block in split_result_blocks(page):
        title_html = _H3.search(block)
        if not title_html:
            continue
        inner = title_html.group(1)
        link_match = _LINK.search(inner)
        url = html_mod.unescape(link_match.group(1)) if link_match else ""
        title = _strip_tags(inner)
        title = re.sub(r"^\[(PDF|BOOK|CITATION|B|HTML)\]\s*", "", title, flags=re.I).strip()
        if not title:
            continue

        authors: list[str] = []
        venue = ""
        year = None
        gs_a = _GS_A.search(block)
        if gs_a:
            meta = _strip_tags(gs_a.group(1))
            parts = [p.strip() for p in meta.split(" - ")]
            if parts:
                authors = [a.strip() for a in parts[0].split(",") if a.strip()]
            if len(parts) >= 2:
                venue_year = parts[1]
                ym = _YEAR.search(venue_year)
                if ym:
                    year = int(ym.group(1))
                    venue = venue_year.replace(ym.group(1), "").strip(" ,;")
                else:
                    venue = venue_year
            if len(parts) >= 3 and not url:
                url = parts[2]
        if year is None:
            ym = _YEAR.search(_strip_tags(block[:2000]))
            year = int(ym.group(1)) if ym else None

        snippet = ""
        gs_rs = _GS_RS.search(block)
        if gs_rs:
            snippet = _strip_tags(gs_rs.group(1))
        citations = None
        cited = _CITED.search(_strip_tags(block))
        if cited:
            citations = int(cited.group(1).replace(",", ""))

        if not venue:
            # Scholar 有时把会议/期刊放在 gs_a 之外（如 "[C] ..."），保持为空而不是编造
            venue = ""

        papers.append(
            Paper(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                url=url,
                abstract=snippet,
                citations=citations,
                item_type=_guess_type(venue, url),
                source="google_scholar",
                sources=["google_scholar"],
                extra={"hl": hl, "snippet_is_abstract": False},
            )
        )
    return papers


def _guess_type(venue: str, url: str) -> str:
    lowered = f"{venue} {url}".lower()
    if "arxiv" in lowered or "preprint" in lowered:
        return "preprint"
    if any(word in lowered for word in ("proceedings", "conference", "symposium", "workshop")):
        return "conferencePaper"
    return "journalArticle"


class GoogleScholarSource(Source):
    name = "google_scholar"
    label = "Google Scholar"
    homepage = "https://scholar.google.com"
    kinds = ("journal", "conference", "preprint")

    def search(self, query, *, limit=None, year_from=None, year_to=None):
        limit = limit or self.limit
        endpoint = self.section.get("endpoint", "https://scholar.google.com/scholar")
        hl = self.section.get("hl", "en")
        per_page = int(self.section.get("results_per_page", 20))
        max_pages = max(1, int(self.section.get("max_pages", 1)))
        results: list[Paper] = []
        seen_titles: set[str] = set()

        for page in range(max_pages):
            params = {"q": query, "hl": hl, "as_sdt": "0,5", "start": page * per_page}
            if year_from:
                params["as_ylo"] = year_from
            if year_to:
                params["as_yhi"] = year_to
            try:
                page_html = self.http.get_text(
                    endpoint,
                    params=params,
                    accept="text/html,application/xhtml+xml",
                    headers={"Referer": f"https://scholar.google.com/scholar?q={quote(query)}"},
                )
            except Exception as exc:  # noqa: BLE001
                if results:
                    break
                raise exc
            if "not a robot" in page_html or "unusual traffic" in page_html.lower():
                raise RuntimeError("Google Scholar 触发了人机校验，请降低频率或稍后再试")
            batch = parse_scholar_html(page_html, hl=hl)
            if not batch:
                break
            for paper in batch:
                if paper.title in seen_titles:
                    continue
                seen_titles.add(paper.title)
                results.append(paper)
            if len(results) >= limit:
                break
        return [p for p in results if year_in_range(p.year, year_from, year_to)][:limit]
