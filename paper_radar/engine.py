"""编排层：把「方向画像 → 多源检索 → 去重排序 → LLM 卡片 → 入库/发信」串起来。

UI（server.py）、命令行（__main__.py）、定时任务（scheduler.py）都只调用这里，
所以"网页点一下"和"每天自动跑"走的是**同一套逻辑**，不会出现两套行为。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import rank as ranking
from .config import Config, get_config
from .mailer import Mailer
from .models import Paper, Recommendation, Seed, Topic, normalize_title
from .net import Fetcher, FetchError
from .sources import build_sources
from .sources.arxiv import ArxivSource
from .sources.openalex import OpenAlexSource, work_to_paper
from .store import Store, now_iso
from .summarize import LLMClient, heuristic_profile
from .zotero import ZoteroClient

Progress = Callable[[str], None]

_ARXIV_ID_RE = re.compile(r"(?:\barxiv:)?(\d{4}\.\d{4,5})(v\d+)?", re.I)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def _noop(_msg: str) -> None:
    return None


def title_similarity(left: str, right: str) -> float:
    """标题相似度：归一化后取 (difflib 比值, token 覆盖率) 的较大者。

    只看 token 覆盖会漏掉词序差异，只看 difflib 又对长短标题不敏感，所以两者取大。
    """
    import difflib

    a = re.sub(r"[^a-z0-9]+", " ", (left or "").lower()).strip()
    b = re.sub(r"[^a-z0-9]+", " ", (right or "").lower()).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    coverage = len(ta & tb) / max(1, min(len(ta), len(tb)))
    if a in b or b in a:
        ratio = max(ratio, 0.95)
    return max(ratio, coverage)


class Context:
    """一次进程内的所有共享对象。UI / CLI / 守护进程都用它，避免各自 new 一遍。"""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self.fetcher = Fetcher(self.cfg)
        self.store = Store(self.cfg.db_path())
        self.llm = LLMClient(self.cfg, self.fetcher)
        self.mailer = Mailer(self.cfg)
        self.zotero = ZoteroClient(self.cfg, self.fetcher)

    def reload(self) -> None:
        """UI 改了配置后调用：重建依赖配置的对象（数据库连接保持）。"""
        get_config(reload=True)
        self.cfg = get_config()
        self.fetcher = Fetcher(self.cfg)
        self.llm = LLMClient(self.cfg, self.fetcher)
        self.mailer = Mailer(self.cfg)
        self.zotero = ZoteroClient(self.cfg, self.fetcher)


class Engine:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    # ================================================================== #
    # 种子论文解析
    # ================================================================== #
    def resolve_seed(self, seed: Seed) -> Paper | None:
        """把用户给的一条输入（标题 / DOI / arXiv ID / URL）解析成标准 Paper。"""
        raw = (seed.value or seed.title or "").strip()
        if seed.doi:
            raw = seed.doi
        if not raw and not seed.arxiv_id:
            return None

        arxiv_id = seed.arxiv_id or ""
        match = _ARXIV_ID_RE.search(raw)
        if not arxiv_id and match and "arxiv" in raw.lower():
            arxiv_id = match.group(1)

        doi = seed.doi or ""
        if not doi:
            doi_match = _DOI_RE.search(raw)
            if doi_match:
                doi = doi_match.group(1).rstrip(".")

        if doi:
            paper = self._paper_by_doi(doi)
            if paper:
                return paper
        if arxiv_id:
            paper = self._paper_by_arxiv(arxiv_id)
            if paper:
                return paper
        return self._paper_by_title(raw)

    def _paper_by_doi(self, doi: str) -> Paper | None:
        try:
            data = self.ctx.fetcher.get_json(
                self.ctx.cfg.get("sources.openalex.endpoint", "https://api.openalex.org/works"),
                params={"filter": f"doi:{doi}", "per-page": 1},
            )
            for work in (data or {}).get("results") or []:
                return work_to_paper(work)
        except Exception:  # noqa: BLE001
            return None
        return None

    def _paper_by_arxiv(self, arxiv_id: str) -> Paper | None:
        source = ArxivSource(self.ctx.cfg, self.ctx.fetcher)
        endpoint = self.ctx.cfg.get("sources.arxiv.endpoint", "https://export.arxiv.org/api/query")
        try:
            xml_text = self.ctx.fetcher.get_text(
                endpoint, params={"id_list": arxiv_id, "max_results": 1}
            )
            from .sources.arxiv import parse_arxiv_feed

            papers = parse_arxiv_feed(xml_text)
            if papers:
                return papers[0]
        except Exception:  # noqa: BLE001
            pass
        # arXiv API 不可用时退回 OpenAlex 的 arXiv 镜像记录
        try:
            data = self.ctx.fetcher.get_json(
                self.ctx.cfg.get("sources.openalex.endpoint", "https://api.openalex.org/works"),
                params={"filter": f"locations.landing_page_url:arxiv.org/abs/{arxiv_id}", "per-page": 1},
            )
            for work in (data or {}).get("results") or []:
                return work_to_paper(work)
        except Exception:  # noqa: BLE001
            pass
        del source
        return None

    def _paper_by_title(self, title: str) -> Paper | None:
        """按标题解析种子论文。

        关键：学术库（尤其 OpenAlex）对标题查询是**模糊匹配**，会返回不相关的结果，
        所以这里必须过一道相似度闸门，宁可解析不出来，也不能把别的论文塞给用户。
        """
        title = (title or "").strip()
        if not title:
            return None
        threshold = float(self.ctx.cfg.get("profile.seed_title_similarity", 0.72))

        # ① OpenAlex 标题检索，取前 5 挑最像的
        try:
            data = self.ctx.fetcher.get_json(
                self.ctx.cfg.get("sources.openalex.endpoint", "https://api.openalex.org/works"),
                params={"search": title[:200], "per-page": 5},
            )
            best: Paper | None = None
            best_ratio = 0.0
            for work in (data or {}).get("results") or []:
                paper = work_to_paper(work)
                if not paper:
                    continue
                ratio = title_similarity(title, paper.title)
                if ratio > best_ratio:
                    best, best_ratio = paper, ratio
            if best and best_ratio >= threshold:
                return best
        except Exception:  # noqa: BLE001
            pass

        # ② 退一步用 arXiv 的标题字段检索（ti:），同样校验相似度
        try:
            from .sources.arxiv import parse_arxiv_feed

            xml_text = self.ctx.fetcher.get_text(
                self.ctx.cfg.get("sources.arxiv.endpoint", "https://export.arxiv.org/api/query"),
                params={"search_query": f'ti:"{title[:180]}"', "max_results": 5},
            )
            best, best_ratio = None, 0.0
            for paper in parse_arxiv_feed(xml_text):
                ratio = title_similarity(title, paper.title)
                if ratio > best_ratio:
                    best, best_ratio = paper, ratio
            if best and best_ratio >= threshold:
                return best
        except Exception:  # noqa: BLE001
            pass
        return None

    # ================================================================== #
    # 方向画像
    # ================================================================== #
    def build_profile(self, topic: Topic, *, progress: Progress = _noop) -> Topic:
        """把"文字概要 + 种子论文"变成 关键词 / 检索式 / 方向描述。"""
        cfg = self.ctx.cfg
        seed_papers: list[Paper] = []
        if topic.seeds:
            progress(f"解析 {len(topic.seeds)} 篇种子论文…")
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(self.resolve_seed, s): s for s in topic.seeds}
                for future in as_completed(futures):
                    seed = futures[future]
                    try:
                        paper = future.result()
                    except Exception as exc:  # noqa: BLE001
                        progress(f"种子解析失败：{seed.value}（{exc}）")
                        continue
                    if paper:
                        seed.title = paper.title
                        seed.doi = paper.doi
                        seed.arxiv_id = paper.arxiv_id
                        seed.url = paper.url
                        seed_papers.append(paper)
                    else:
                        progress(f"未能解析种子：{seed.value}")

        seed_texts = [
            f"{p.title} {p.abstract[:600] if p.abstract else ''}" for p in seed_papers
        ]
        # 没能解析出元数据的种子也要参与关键词抽取，否则用户写的标题就白填了
        unresolved = [s.value for s in topic.seeds if s.value and not s.title]
        seed_texts.extend(unresolved)
        seed_payload = [p.to_dict() for p in seed_papers] + [
            {"title": value, "venue": "", "year": None} for value in unresolved
        ]
        max_keywords = int(cfg.get("profile.max_keywords", 20))
        max_queries = int(cfg.get("profile.max_queries", 6))

        profile: dict[str, Any] = {}
        if self.ctx.llm.enabled_for("profile"):
            progress(f"调用 {cfg.get('llm.provider')} 生成检索方案…")
            profile = self.ctx.llm.build_profile(
                direction=topic.direction,
                seeds=seed_payload,
                max_queries=max_queries,
            )
            if not profile:
                progress("LLM 未返回有效结果，改用启发式关键词抽取。")
        if not profile:
            profile = heuristic_profile(
                topic.direction,
                seed_texts,
                max_keywords=max_keywords,
                max_queries=max_queries,
                stopwords=set(cfg.get("profile.stopwords", []) or []),
            )

        # 用户显式填过的关键词 / 检索式优先，不被覆盖
        if not topic.keywords and profile.get("keywords"):
            topic.keywords = profile["keywords"][:max_keywords]
        if not topic.queries and profile.get("queries"):
            topic.queries = profile["queries"][:max_queries]
        if not topic.negative_keywords and profile.get("negative_keywords"):
            topic.negative_keywords = profile["negative_keywords"]
        if profile.get("profile_summary"):
            topic.profile_summary = profile["profile_summary"]
        if not topic.queries and topic.keywords:
            topic.queries = [" AND ".join(f'"{k}"' for k in topic.keywords[:3])]
        topic.updated_at = now_iso()
        topic.id = self.ctx.store.save_topic(topic)
        self.ctx.store.upsert_papers(seed_papers)
        progress(
            f"完成：{len(topic.keywords)} 个关键词 / {len(topic.queries)} 条检索式"
            f"（来源：{profile.get('provider', 'heuristic')}）"
        )
        return topic

    # ================================================================== #
    # 检索
    # ================================================================== #
    def summarize_papers(
        self, topic: Topic, papers: list[Paper], *, progress: Progress = _noop
    ) -> tuple[list[Recommendation], str]:
        """为已排好序的论文生成"内容概要 / 文章特色 / 推荐理由"。"""
        cfg = self.ctx.cfg
        cards: dict[str, Any] = {}
        provider = "heuristic"
        if papers and self.ctx.llm.enabled_for("summary"):
            progress(f"生成推荐卡片（{len(papers)} 篇，模型 {cfg.get('llm.provider')}）…")
            cards = self.ctx.llm.summarize(
                papers, direction=topic.direction or topic.name, keywords=topic.keywords
            )
            provider = cfg.get("llm.provider", "") or "heuristic"
        elif papers:
            progress("LLM 摘要已关闭，使用启发式卡片。")

        from .summarize import _heuristic_card

        recs: list[Recommendation] = []
        for paper in papers:
            card = cards.get(paper.key)
            rec = Recommendation(
                paper=paper,
                score=paper.score,
                score_parts=paper.score_parts,
                summary=card.summary if card else "",
                highlights=card.highlights if card else [],
                reason=card.reason if card else "",
            )
            if not rec.summary:
                fallback = _heuristic_card(paper, topic.direction or topic.name, topic.keywords)
                rec.summary, rec.highlights, rec.reason = (
                    fallback.summary,
                    fallback.highlights,
                    fallback.reason,
                )
                provider = "heuristic"
            recs.append(rec)
        return recs, provider

    def collect(
        self,
        topic: Topic,
        *,
        limit: int | None = None,
        sources: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_citations: int | None = None,
        progress: Progress = _noop,
    ) -> dict[str, Any]:
        """并发检索 + 去重排序，返回 Paper 对象。不调用 LLM、不写库，便于每日简报先过滤再花钱。"""
        cfg = self.ctx.cfg
        queries = [q for q in (topic.queries or []) if q.strip()] or [topic.direction or topic.name]
        plugins = build_sources(cfg, self.ctx.fetcher, only=sources)
        if not plugins:
            return {"papers": [], "errors": {"_": "没有可用的数据源"}, "candidates": 0, "sources": [], "queries": queries}

        if year_from is None:
            lookback = cfg.get("search.year_lookback", 5)
            year_from = int(cfg.get("search.year_from") or 0) or (
                datetime.now(timezone.utc).year - int(lookback) if lookback else None
            )
        limit = limit or int(cfg.get("search.top_n", 15))
        per_source = int(cfg.get("sources.per_source_limit", 20))

        collected: list[Paper] = []
        errors: dict[str, str] = {}
        jobs = [(plugin, query) for plugin in plugins for query in queries]
        progress(f"并发检索：{len(queries)} 条检索式 × {len(plugins)} 个数据源 = {len(jobs)} 个任务")

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
            futures = {
                pool.submit(
                    plugin.search,
                    query,
                    limit=per_source,
                    year_from=year_from,
                    year_to=year_to,
                ): (plugin, query)
                for plugin, query in jobs
            }
            for future in as_completed(futures):
                plugin, _query = futures[future]
                try:
                    found = future.result() or []
                    collected.extend(found)
                    progress(f"  {plugin.label} · {len(found)} 条")
                except FetchError as exc:
                    errors[plugin.name] = f"{exc}"
                    progress(f"  {plugin.label} 失败：{exc}")
                except Exception as exc:  # noqa: BLE001
                    errors[plugin.name] = f"{type(exc).__name__}: {exc}"
                    progress(f"  {plugin.label} 失败：{exc}")

        candidates = len(collected)
        ranked = ranking.rank(
            collected,
            cfg=cfg,
            keywords=topic.keywords,
            negative_keywords=topic.negative_keywords,
            year_from=year_from,
            year_to=year_to,
            min_citations=min_citations,
            exclude_venues=topic.filters.get("exclude_venues") or cfg.get("search.exclude_venues"),
            limit=min(limit, int(cfg.get("search.max_candidates", 150))),
        )
        progress(f"去重后 {len(ranking.dedupe(collected))} 篇，选出 {len(ranked)} 篇")
        ranked, excluded = self.exclude_known(topic, ranked)
        if excluded:
            progress(f"排除已知论文 {excluded} 篇（方向种子 / 已入过 Zotero 的）")
        self._canonicalize(ranked)
        return {
            "papers": ranked,
            "candidates": candidates,
            "errors": errors,
            "sources": [p.name for p in plugins],
            "queries": queries,
            "year_from": year_from,
        }

    def exclude_known(self, topic: Topic, papers: list[Paper]) -> tuple[list[Paper], int]:
        """剔除用户**已经知道**的论文：方向的种子论文 + 已经入过 Zotero 的。

        种子论文是用户自己提供的"已知工作"，被数据源反复抓到是常态；
        不排除就会被当成新论文推荐、甚至发进每日简报 —— 用户会收到自己早就给过的东西。
        两个开关都在 config（search.exclude_seeds / search.exclude_already_added）。
        """
        cfg = self.ctx.cfg
        index = ranking.seed_index(topic.seeds) if cfg.get("search.exclude_seeds", True) else None
        linked: dict = {}
        if cfg.get("search.exclude_already_added", True):
            try:
                linked = self.ctx.store.zotero_links()
            except Exception:  # noqa: BLE001 - 排除失败不该影响检索
                linked = {}
        if not index and not linked:
            return papers, 0
        kept: list[Paper] = []
        for paper in papers:
            if index and ranking.is_seed_paper(paper, index):
                continue
            if linked and paper.key in linked:
                continue
            kept.append(paper)
        return kept, len(papers) - len(kept)

    def _canonicalize(self, papers: list[Paper]) -> None:
        """把本轮结果对齐到本地库里已存在的记录上（按归一化标题判定同一篇论文）。

        为什么必须在**写库之前**做：不同数据源给同一篇论文的标识不一样
        （Google Scholar 没有 DOI，Crossref/OpenAlex 有）。若每轮各按自己的指纹主键写入，
        同一篇论文会留下 `title:...` 和 `doi:...` 两行 —— 表现为推荐表/高分记忆里出现重复条目，
        甚至把已经看过的论文当成新论文再推一次。
        """
        index = self.ctx.store.title_key_index()
        for paper in papers:
            norm = normalize_title(paper.title)
            existing = index.get(norm)
            if existing and existing != paper.key:
                paper.canonical_key = existing
            else:
                index.setdefault(norm, paper.key)

    def search(
        self,
        topic: Topic,
        *,
        summarize: bool = True,
        record: bool = True,
        progress: Progress = _noop,
        **kwargs: Any,
    ) -> dict[str, Any]:
        cfg = self.ctx.cfg
        limit = int(kwargs.get("limit") or cfg.get("search.top_n", 15))
        rerank = (
            summarize
            and bool(cfg.get("search.rerank", True))
            and self.ctx.llm.enabled_for("relevance")
        )
        if rerank:
            # 多取一倍候选，让语义重排有机会把真正相关但规则分偏低的论文提上来
            kwargs["limit"] = min(int(cfg.get("search.max_candidates", 150)), limit * 2)

        collected = self.collect(topic, progress=progress, **kwargs)
        ranked: list[Paper] = collected["papers"]
        if rerank:
            ranked = self.rerank_with_llm(topic, ranked, progress=progress)[:limit]
        self.ctx.store.upsert_papers(ranked)

        if summarize:
            recs, provider = self.summarize_papers(topic, ranked, progress=progress)
        else:
            recs, provider = [Recommendation(paper=p) for p in ranked], "skipped"

        if record and topic.id and recs:
            self.ctx.store.save_recommendations(topic.id, recs, status="shown", provider=provider)

        remembered: list[dict] = []
        if bool(cfg.get("remember.apply_to_search", True)):
            remembered = self.remember_high_scores(topic, recs, origin="search", progress=progress)
        remembered_keys = {item["key"] for item in remembered} | self.remembered_key_set()
        threshold = self.remember_threshold()

        return {
            "topic": topic.to_dict(),
            "papers": [
                {**rec.to_dict(), "remembered": rec.paper.key in remembered_keys}
                for rec in recs
            ],
            "candidates": collected.get("candidates", len(ranked)),
            "errors": collected.get("errors", {}),
            "sources": collected.get("sources", []),
            "queries": collected.get("queries", []),
            "year_from": collected.get("year_from"),
            "provider": provider,
            "reranked": rerank,
            "remembered": remembered,
            "remember_threshold": threshold,
        }

    # ================================================================== #
    # 高分记忆
    # ================================================================== #
    def remember_threshold(self) -> float:
        return float(self.ctx.cfg.get("remember.min_score", 0.6) or 0.0)

    def remembered_key_set(self) -> set[str]:
        """只取"分数仍达标"的记忆键，供 UI 打标记；阈值调高后旧标记会自动消失。"""
        return self.ctx.store.remembered_keys(min_score=self.remember_threshold())

    def remember_high_scores(
        self,
        topic: Topic,
        recs: list[Recommendation],
        *,
        origin: str = "search",
        progress: Progress = _noop,
    ) -> list[dict]:
        """把相关度达阈值的论文记入长期记忆；可选地把它们反过来喂给检索方向。"""
        cfg = self.ctx.cfg
        if not cfg.get("remember.enabled", True) or not recs:
            return []
        threshold = self.remember_threshold()
        added = self.ctx.store.remember(topic, recs, min_score=threshold, origin=origin)
        if added:
            progress(
                f"高分记忆 +{len(added)} 篇（相关度 ≥ {threshold}）："
                + "、".join(item["title"][:36] for item in added[:3])
            )
        if added and cfg.get("remember.feedback_as_seeds", False):
            self._feed_back_as_seeds(topic, added, progress=progress)
        return added

    def _feed_back_as_seeds(self, topic: Topic, added: list[dict], *, progress: Progress = _noop) -> None:
        """把高分论文追加为该方向的种子论文，让下一次方向画像更贴近你真正认可的工作。

        默认关闭（`remember.feedback_as_seeds`）：它会悄悄改变后续检索结果，
        属于"要让用户显式开启"的行为。种子总数上限由 `remember.feedback_max_seeds` 控制。
        """
        if not topic.id:
            return
        cap = int(self.ctx.cfg.get("remember.feedback_max_seeds", 5) or 5)
        stored = self.ctx.store.get_topic(topic.id)
        if not stored:
            return
        known = {s.doi or s.arxiv_id or s.title for s in stored.seeds}
        room = max(0, cap - len(stored.seeds))
        if room == 0:
            progress(f"种子论文已达上限 {cap} 篇，跳过回填（可调 remember.feedback_max_seeds）")
            return

        added_count = 0
        for item in added:
            if added_count >= room:
                break
            paper = self.ctx.store.get_paper(item["key"])
            if not paper:
                continue
            ident = paper.doi or paper.arxiv_id or paper.title
            if ident in known:
                continue
            stored.seeds.append(
                Seed(value=ident, title=paper.title, doi=paper.doi, arxiv_id=paper.arxiv_id, url=paper.url)
            )
            known.add(ident)
            added_count += 1
        if added_count:
            self.ctx.store.save_topic(stored)
            topic.seeds = stored.seeds
            progress(f"已把 {added_count} 篇高分论文回填为种子（该方向种子上限 {cap} 篇）")


    # ================================================================== #
    # 一键入库
    # ================================================================== #
    def rerank_with_llm(
        self, topic: Topic, papers: list[Paper], *, progress: Progress = _noop
    ) -> list[Paper]:
        """可选的语义重排：把 LLM 的相关性判断与规则分融合。

        只对已经过规则筛选的小候选集（十来篇）调用，成本可控；
        `llm.enabled_for.relevance = false` 时原样返回。
        """
        if not papers or not self.ctx.llm.enabled_for("relevance"):
            return papers
        weight = float(self.ctx.cfg.get("search.llm_relevance_weight", 0.45))
        progress(f"语义重排 {len(papers)} 篇候选…")
        scores = self.ctx.llm.score_relevance(
            papers, direction=topic.direction or topic.name, keywords=topic.keywords
        )
        if not scores:
            progress("语义重排未返回结果，沿用规则分。")
            return papers
        for paper in papers:
            semantic = scores.get(paper.key)
            if semantic is None:
                continue
            paper.extra["llm_relevance"] = round(semantic, 3)
            paper.score = (1 - weight) * paper.score + weight * semantic
            paper.score_parts = {**paper.score_parts, "llm_relevance": semantic}
        return sorted(papers, key=lambda p: -p.score)

    def add_to_zotero(
        self, keys: list[str], *, collection: str = "", tags: list[str] | None = None, topic_id: int | None = None
    ) -> dict:
        papers: list[Paper] = []
        result: dict[str, Any] = {"added": 0, "duplicates": 0, "failed": 0, "items": [], "error": ""}
        for key in keys:
            paper = self.ctx.store.get_paper(key)
            if not paper:
                continue
            # 第一层：本地记录（立刻幂等，不受 Zotero 搜索索引延迟影响）
            if self.ctx.store.is_linked_zotero(key):
                result["duplicates"] += 1
                result["items"].append({"key": key, "title": paper.title, "status": "duplicate"})
                continue
            papers.append(paper)

        if papers:
            remote = self.ctx.zotero.add_papers(papers, collection=collection, tags=tags)
            result["added"] = remote.get("added", 0)
            result["duplicates"] += remote.get("duplicates", 0)
            result["failed"] = remote.get("failed", 0)
            result["error"] = remote.get("error", "")
            result["items"].extend(remote.get("items", []))
            # 第二层：把成功写入的记到本地
            for entry in remote.get("items", []):
                if entry.get("status") == "added":
                    self.ctx.store.link_zotero(entry["key"], entry.get("zotero_key", ""), entry.get("title", ""))
        if not papers and not result["items"]:
            result["error"] = "本地库里找不到这些论文"
        if topic_id:
            for entry in result.get("items", []):
                if entry.get("status") == "added":
                    self.ctx.store.set_status(topic_id, entry["key"], "accepted")
        return result


def default_topic_from_payload(payload: dict) -> Topic:
    """把 UI / CLI 传来的 dict 变成 Topic（宽松解析，缺字段就留空）。"""
    seeds = []
    for raw in payload.get("seeds") or []:
        if isinstance(raw, str):
            seeds.append(Seed(value=raw.strip()))
        elif isinstance(raw, dict):
            seeds.append(
                Seed(
                    value=str(raw.get("value") or raw.get("title") or "").strip(),
                    title=raw.get("title", "") or "",
                    doi=raw.get("doi", "") or "",
                    arxiv_id=raw.get("arxiv_id", "") or "",
                    url=raw.get("url", "") or "",
                )
            )
    return Topic(
        id=payload.get("id") or None,
        name=(payload.get("name") or "").strip() or "未命名方向",
        direction=(payload.get("direction") or "").strip(),
        seeds=[s for s in seeds if s.value or s.doi or s.arxiv_id],
        keywords=[str(k).strip() for k in (payload.get("keywords") or []) if str(k).strip()],
        negative_keywords=[str(k).strip() for k in (payload.get("negative_keywords") or []) if str(k).strip()],
        queries=[str(q).strip() for q in (payload.get("queries") or []) if str(q).strip()],
        filters=payload.get("filters") or {},
        zotero_collection=payload.get("zotero_collection") or "",
        enabled=payload.get("enabled", True),
        profile_summary=payload.get("profile_summary") or "",
    )


def sleep_with_progress(seconds: float) -> None:  # pragma: no cover - 调试辅助
    time.sleep(seconds)


def days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
