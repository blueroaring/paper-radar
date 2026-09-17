"""去重、过滤与相关性排序。

打分公式（权重全部来自 config.search.weights，改配置即可调风格）：

    score = Σ wᵢ · partᵢ / Σ wᵢ
    part_relevance : 关键词命中（标题权重最高），命中越多越接近 1
    part_recency   : 2^(-年龄/半衰期)，越新越高
    part_citations : log(1+引用数)/log(1+饱和值)，避免高引论文一家独大
    part_venue     : 发表处分区（S/A/B/未知，分区表在 config 里可随意改成 CCF 或自己的清单）
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from .models import Paper, normalize_title

_TIER_CACHE: dict[str, list[tuple[str, str, re.Pattern]]] = {}


def _tier_rules(cfg) -> list[tuple[str, str, re.Pattern]]:
    """把 config 的分区表编译成 (tier, 原始词, 正则) 列表，带缓存。"""
    tiers = cfg.get("search.venue_tiers", {}) or {}
    cache_key = json.dumps(tiers, sort_keys=True, ensure_ascii=False)
    if cache_key in _TIER_CACHE:
        return _TIER_CACHE[cache_key]
    rules: list[tuple[str, str, re.Pattern]] = []
    for tier, names in tiers.items():
        for name in names or []:
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(str(name))}(?![A-Za-z0-9])", re.I)
            rules.append((str(tier), str(name), pattern))
    _TIER_CACHE[cache_key] = rules
    return rules


def venue_tier(paper: Paper, cfg) -> tuple[str, float]:
    haystack = " ".join(
        filter(None, [paper.venue, paper.venue_detail, str(paper.extra.get("dblp_type", ""))])
    )
    if not haystack:
        return "unknown", float(cfg.get("search.tier_score.unknown", 0.2))
    best_tier = "unknown"
    best_score = float(cfg.get("search.tier_score.unknown", 0.2))
    for tier, _name, pattern in _tier_rules(cfg):
        if pattern.search(haystack):
            score = float(cfg.get(f"search.tier_score.{tier}", 0.2))
            if score > best_score:
                best_tier, best_score = tier, score
    return best_tier, best_score


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #


def dedupe(papers: list[Paper]) -> list[Paper]:
    """合并同一篇论文的多条记录。

    两级判定：
      1. 有 DOI / arXiv ID 时用指纹（最可靠）；
      2. 再加一道**归一化标题完全相同**的合并 —— Google Scholar 拿不到 DOI，
         只有靠标题才能和 OpenAlex/dblp 的记录对上，否则推荐表里会出现重复条目。
    """
    merged: dict[str, Paper] = {}
    by_title: dict[str, str] = {}
    for paper in papers:
        if not paper.title:
            continue
        key = paper.key
        ntitle = normalize_title(paper.title)
        target_key = key if key in merged else by_title.get(ntitle)
        if target_key is None:
            merged[key] = paper
            by_title[ntitle] = key
            continue
        obj = merged[target_key]
        if obj is not paper:
            obj.merge(paper)
        merged[key] = obj
        by_title.setdefault(ntitle, key)

    seen: set[int] = set()
    out: list[Paper] = []
    for obj in merged.values():
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        out.append(obj)
    return out


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #


def apply_filters(
    papers: list[Paper],
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int = 0,
    exclude_venues: list[str] | None = None,
    require_title_keyword: bool = False,
    keywords: list[str] | None = None,
) -> list[Paper]:
    excludes = [e.lower().strip() for e in (exclude_venues or []) if e and e.strip()]
    out = []
    for paper in papers:
        if year_from and paper.year and paper.year < year_from:
            continue
        if year_to and paper.year and paper.year > year_to:
            continue
        if min_citations and (paper.citations or 0) < min_citations:
            continue
        if excludes:
            venue = (paper.venue or "").lower()
            if any(word in venue for word in excludes):
                continue
        if require_title_keyword and keywords:
            title = paper.title.lower()
            if not any(k.lower() in title for k in keywords):
                continue
        out.append(paper)
    return out


# --------------------------------------------------------------------------- #
# 关键词命中
# --------------------------------------------------------------------------- #


def match_keywords(paper: Paper, keywords: list[str]) -> tuple[list[str], float]:
    """返回 (命中的关键词, 原始相关性得分)。长关键词权重更高（短语比单词更有信息量）。"""
    if not keywords:
        return [], 0.0
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    venue = (paper.venue or "").lower()
    hits: list[str] = []
    raw = 0.0
    for keyword in keywords:
        kw = (keyword or "").strip().lower()
        if len(kw) < 2:
            continue
        weight = 1.0 + min(1.0, len(kw.split()) * 0.5)
        if kw in title:
            raw += 3.0 * weight
            hits.append(keyword)
        elif kw in abstract:
            raw += 1.0 * weight
            hits.append(keyword)
        elif kw in venue:
            raw += 0.5 * weight
    return hits, raw


def penalty_keywords(paper: Paper, negative: list[str]) -> float:
    if not negative:
        return 0.0
    text = f"{paper.title} {paper.abstract}".lower()
    hits = sum(1 for word in negative if word and word.lower() in text)
    return min(0.5, 0.15 * hits)


# --------------------------------------------------------------------------- #
# 已知论文的排除（种子论文 / 已入库）
# --------------------------------------------------------------------------- #

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
# arXiv ID 形如 YYMM.NNNNN，年月必须合法。
# 加这两道约束是因为 DOI 里的数字串会被误判成 arXiv ID：
#   10.1145/3689031.3717495  →  曾错误匹配出 "9031.37174"（月份 31 不存在）
_ARXIV_RE = re.compile(r"(?<![\d.])(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5})(?:v\d+)?(?![\d.])", re.I)


def seed_index(seeds) -> dict[str, set[str]]:
    """把方向的种子论文归一化成可比较的标识集合（DOI / arXiv ID / 归一化标题）。

    为什么需要：用户给的种子论文正是他**已经知道**的工作。数据源每轮都可能再抓到它，
    不排除的话就会被当成"新论文"反复推荐、甚至发进简报（线上真实故障）。
    """
    index: dict[str, set[str]] = {"doi": set(), "arxiv": set(), "title": set()}
    for seed in seeds or []:
        doi = getattr(seed, "doi", "") or ""
        arxiv_id = getattr(seed, "arxiv_id", "") or ""
        title = getattr(seed, "title", "") or ""
        value = getattr(seed, "value", "") or ""
        blob = " ".join(filter(None, [doi, arxiv_id, title, value]))
        for match in _DOI_RE.findall(blob):
            index["doi"].add(match.rstrip(".").lower())
        for match in _ARXIV_RE.findall(blob):
            index["arxiv"].add(match.lower())
        norm = normalize_title(title)
        if not norm and value and not _DOI_RE.search(value) and not _ARXIV_RE.fullmatch(value.strip()):
            norm = normalize_title(value)
        # 至少三个词才当标题，避免把 DOI 串之类误当标题
        if len(norm.split()) >= 3:
            index["title"].add(norm)
    return index


def is_seed_paper(paper: Paper, index: dict[str, set[str]]) -> bool:
    doi = (paper.doi or "").strip().lower()
    if doi and doi in index["doi"]:
        return True
    arxiv_id = re.sub(r"v\d+$", "", (paper.arxiv_id or "").strip().lower())
    if arxiv_id and arxiv_id in index["arxiv"]:
        return True
    norm = normalize_title(paper.title)
    return bool(norm) and norm in index["title"]


# --------------------------------------------------------------------------- #
# 主排序
# --------------------------------------------------------------------------- #


def score_papers(
    papers: list[Paper],
    *,
    keywords: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    cfg,
    now_year: int | None = None,
) -> list[Paper]:
    weights = cfg.get("search.weights", {}) or {}
    w_rel = float(weights.get("relevance", 1.0))
    w_rec = float(weights.get("recency", 0.35))
    w_cit = float(weights.get("citations", 0.22))
    w_ven = float(weights.get("venue", 0.18))
    total_w = max(1e-6, w_rel + w_rec + w_cit + w_ven)

    half_life = max(0.5, float(cfg.get("search.recency_half_life_years", 2.5)))
    saturation = max(1.0, float(cfg.get("search.citation_saturation", 200)))
    title_w = float(cfg.get("profile.keywords_weight_title", 3.0))
    scale = max(1e-6, title_w * 3.0)  # 标题命中 3 个关键词即视为满分相关
    now_year = now_year or datetime.now(timezone.utc).year

    for paper in papers:
        hits, raw = match_keywords(paper, keywords or [])
        rel = min(1.0, raw / scale)
        rel = max(0.0, rel - penalty_keywords(paper, negative_keywords or []))

        age = max(0, now_year - (paper.year or now_year))
        if paper.year is None:
            rec = 0.3
        else:
            rec = 2 ** (-age / half_life)

        cit = math.log1p(max(0, paper.citations or 0)) / math.log1p(saturation)
        cit = min(1.0, cit)

        tier, ven = venue_tier(paper, cfg)

        parts = {"relevance": rel, "recency": rec, "citations": cit, "venue": ven}
        score = (w_rel * rel + w_rec * rec + w_cit * cit + w_ven * ven) / total_w

        paper.score = score
        paper.score_parts = parts
        paper.matched_keywords = hits
        paper.extra["venue_tier"] = tier

    return sorted(papers, key=lambda p: (-p.score, -(p.citations or 0), p.title))


def rank(
    papers: list[Paper],
    *,
    cfg,
    keywords: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    exclude_venues: list[str] | None = None,
    require_title_keyword: bool | None = None,
    limit: int | None = None,
) -> list[Paper]:
    """一次走完 去重 → 过滤 → 打分 → 截断。"""
    if min_citations is None:
        min_citations = int(cfg.get("search.min_citations", 0) or 0)
    if require_title_keyword is None:
        require_title_keyword = bool(cfg.get("search.require_title_keyword", False))
    cleaned = dedupe(papers)
    cleaned = apply_filters(
        cleaned,
        year_from=year_from,
        year_to=year_to,
        min_citations=min_citations,
        exclude_venues=exclude_venues,
        require_title_keyword=require_title_keyword,
        keywords=keywords,
    )
    scored = score_papers(
        cleaned, keywords=keywords, negative_keywords=negative_keywords, cfg=cfg
    )
    if limit:
        return scored[:limit]
    return scored
