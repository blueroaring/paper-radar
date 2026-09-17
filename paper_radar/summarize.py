"""LLM 层：把"写方向概要 / 写推荐卡片"抽象成可插拔的 provider。

可扩展性设计：
  * 代码里只注册了三种 **style**（openai 兼容 / ollama / heuristic）；
  * 具体厂商（deepseek、moonshot、zhipu、本地模型……）全部写在 config 的 llm.providers 里，
    加一个新厂商 = 加一行配置，不需要改代码；
  * 任何一次调用失败都会自动降级到 fallback_provider，再失败就降级到启发式，
    保证"没有大模型也能出结果"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import textutil


# --------------------------------------------------------------------------- #
# 后端（按 style 注册）
# --------------------------------------------------------------------------- #


class Backend:
    style = ""

    def __init__(self, name: str, spec: dict, cfg, fetcher, api_key: str = ""):
        self.name = name
        self.spec = spec or {}
        self.cfg = cfg
        self.http = fetcher
        self.api_key = api_key or (spec or {}).get("api_key", "")

    def chat(self, system: str, user: str) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError


class OpenAICompatBackend(Backend):
    """任何 OpenAI /chat/completions 兼容的服务：DeepSeek、Moonshot、智谱、vLLM、OneAPI…"""

    style = "openai"

    def chat(self, system: str, user: str) -> str:
        base = (self.spec.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError(f"provider {self.name} 缺少 base_url")
        model = self.spec.get("model") or self.cfg.get("llm.model")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(self.cfg.get("llm.temperature", 0.2)),
            "max_tokens": int(self.cfg.get("llm.max_tokens", 3000)),
            "stream": False,
        }
        if self.cfg.get("llm.json_mode", False):
            payload["response_format"] = {"type": "json_object"}
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, data = self.http.post_json(f"{base}/chat/completions", payload, headers=headers)
        if status >= 400:
            raise RuntimeError(f"{self.name} 返回 {status}: {str(data)[:300]}")
        choices = (data or {}).get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.name} 返回内容为空: {str(data)[:200]}")
        return ((choices[0].get("message") or {}).get("content") or "").strip()


class OllamaBackend(Backend):
    """本地 Ollama，零成本、可离线（国内网络不稳时的兜底）。"""

    style = "ollama"

    def chat(self, system: str, user: str) -> str:
        base = (self.spec.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        payload = {
            "model": self.spec.get("model") or "llama3",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": float(self.cfg.get("llm.temperature", 0.2))},
        }
        status, data = self.http.post_json(f"{base}/api/chat", payload)
        if status >= 400:
            raise RuntimeError(f"ollama 返回 {status}: {str(data)[:300]}")
        return ((data or {}).get("message") or {}).get("content", "").strip()


BACKEND_STYLES: dict[str, type[Backend]] = {
    "openai": OpenAICompatBackend,
    "ollama": OllamaBackend,
}


# --------------------------------------------------------------------------- #
# 卡片 / 结果
# --------------------------------------------------------------------------- #


@dataclass
class PaperCard:
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class TaskResult:
    data: Any
    provider: str = "heuristic"
    error: str = ""


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class LLMClient:
    def __init__(self, cfg, fetcher):
        self.cfg = cfg
        self.http = fetcher
        self._cache: dict[str, Backend | None] = {}

    # ------------------------------------------------------------------ #
    def backend(self, name: str | None = None) -> Backend | None:
        name = name or self.cfg.get("llm.provider", "none") or "none"
        if name in self._cache:
            return self._cache[name]
        providers = self.cfg.get("llm.providers", {}) or {}
        spec = providers.get(name) or {}
        style = spec.get("style", "heuristic")
        cls = BACKEND_STYLES.get(style)
        backend: Backend | None = None
        if cls:
            merged = dict(spec)
            # 顶层 llm.base_url / model / api_key 只作用于 llm.provider 指定的那家，
            # 这样"换一行配置就切换厂商"成立，同时不会污染 fallback provider。
            if name == self.cfg.get("llm.provider"):
                for key in ("base_url", "model", "api_key"):
                    if self.cfg.get(f"llm.{key}"):
                        merged[key] = self.cfg.get(f"llm.{key}")
            backend = cls(name, merged, self.cfg, self.http, api_key=merged.get("api_key", ""))
        self._cache[name] = backend
        return backend

    def enabled_for(self, purpose: str) -> bool:
        return bool(self.cfg.get(f"llm.enabled_for.{purpose}", False))

    def complete(self, system_key: str, user_key: str, purpose: str, **kwargs: Any) -> TaskResult:
        """按 prompt 模板渲染并调用 LLM；失败自动降级。返回值里的 provider 便于 UI 显示来源。"""
        if not self.enabled_for(purpose):
            return TaskResult(None, provider="disabled")
        system = self._render(system_key, kwargs)
        user = self._render(user_key, kwargs)
        chain = [self.cfg.get("llm.provider"), self.cfg.get("llm.fallback_provider")]
        last_error = ""
        for name in chain:
            if not name:
                continue
            backend = self.backend(name)
            if backend is None:
                continue
            try:
                raw = backend.chat(system, user)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{name}: {exc}"
                continue
            parsed = textutil.extract_json_block(raw)
            if parsed is None:
                last_error = f"{name}: 无法从输出中解析 JSON"
                continue
            return TaskResult(parsed, provider=name)
        return TaskResult(None, provider="heuristic", error=last_error)

    def _render(self, key: str, kwargs: dict) -> str:
        template = self.cfg.get(f"prompts.{key}", "") or ""
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template

    # ------------------------------------------------------------------ #
    def build_profile(
        self,
        *,
        direction: str,
        seeds: list[dict],
        max_queries: int = 6,
    ) -> dict:
        """用自然语言方向 + 种子论文，生成 关键词 / 检索式 / 方向画像。"""
        seed_text = "\n".join(
            f"- {s.get('title') or s.get('value', '')} （{s.get('venue') or '未知发表处'} {s.get('year') or ''}）"
            for s in seeds
        ) or "（无）"
        result = self.complete(
            "profile_system",
            "profile_user",
            "profile",
            direction=direction or "（用户未填写文字概要，请仅依据种子论文推断）",
            seeds=seed_text,
            max_queries=max_queries,
        )
        if isinstance(result.data, dict):
            return {
                "profile_summary": str(result.data.get("profile_summary", "")).strip(),
                "keywords": [str(k).strip() for k in (result.data.get("keywords") or []) if str(k).strip()],
                "queries": [str(q).strip() for q in (result.data.get("queries") or []) if str(q).strip()],
                "negative_keywords": [
                    str(k).strip() for k in (result.data.get("negative_keywords") or []) if str(k).strip()
                ],
                "provider": result.provider,
                "error": result.error,
            }
        return {}

    # ------------------------------------------------------------------ #
    def summarize(self, papers: list, *, direction: str, keywords: list[str]) -> dict[str, PaperCard]:
        """批量生成"内容概要 / 文章特色 / 推荐理由"。"""
        cards: dict[str, PaperCard] = {}
        if not papers:
            return cards
        batch_size = max(1, int(self.cfg.get("llm.batch_size", 4)))
        for start in range(0, len(papers), batch_size):
            batch = papers[start : start + batch_size]
            payload = "\n\n".join(_paper_payload(idx + 1, p) for idx, p in enumerate(batch))
            result = self.complete(
                "summary_system",
                "summary_user",
                "summary",
                direction=direction or "（未指定）",
                keywords=", ".join(keywords or []) or "（未指定）",
                count=len(batch),
                papers=payload,
            )
            raw_cards = []
            if isinstance(result.data, dict):
                raw_cards = result.data.get("cards") or []
            by_id = {}
            for card in raw_cards:
                try:
                    by_id[int(card.get("id"))] = card
                except (TypeError, ValueError):
                    continue
            for idx, paper in enumerate(batch, start=1):
                card = by_id.get(idx)
                if card:
                    highlights = card.get("highlights") or []
                    if isinstance(highlights, str):
                        highlights = [highlights]
                    cards[paper.key] = PaperCard(
                        summary=str(card.get("summary", "")).strip(),
                        highlights=[str(h).strip() for h in highlights if str(h).strip()],
                        reason=str(card.get("reason", "")).strip(),
                    )
                else:
                    cards[paper.key] = _heuristic_card(paper, direction, keywords)
        return cards

    # ------------------------------------------------------------------ #
    def propose_origins(
        self, *, direction: str, keywords: list[str], max_origins: int = 4
    ) -> dict:
        """让模型指出这个方向的**奠基性工作**（脉络模式的起点）。

        返回 {"origins": [{title, authors, year, why}], "start_year": int|None, "provider": str}。
        模型可能记错标题/年份，所以调用方**必须**用学术库校验（engine.find_origins 会做）。
        """
        result = self.complete(
            "origin_system",
            "origin_user",
            "profile",  # 与方向画像同一类开销，共用 enabled_for.profile 开关
            direction=direction or "（未指定）",
            keywords=", ".join(keywords or []) or "（未指定）",
            max_origins=max_origins,
        )
        out: dict = {"origins": [], "start_year": None, "provider": result.provider, "error": result.error}
        if isinstance(result.data, dict):
            for raw in result.data.get("origins") or []:
                title = str(raw.get("title") or "").strip()
                if not title:
                    continue
                year = raw.get("year")
                try:
                    year = int(year) if year is not None else None
                except (TypeError, ValueError):
                    year = None
                out["origins"].append(
                    {
                        "title": title,
                        "authors": str(raw.get("authors") or "").strip(),
                        "year": year,
                        "why": str(raw.get("why") or "").strip(),
                    }
                )
            start = result.data.get("start_year")
            try:
                out["start_year"] = int(start) if start is not None else None
            except (TypeError, ValueError):
                out["start_year"] = None
        return out

    # ------------------------------------------------------------------ #
    def score_relevance(self, papers: list, *, direction: str, keywords: list[str]) -> dict[str, float]:
        """让 LLM 给候选打 0~1 的相关性分（用于每日简报的最终精选，压制"关键词蹭到"的噪声）。

        关键词匹配是字面匹配，"data-driven buffer sizing for projects" 这种跨领域论文
        照样能命中 buffer sizing；只有语义判断才能把它们压下去。
        """
        scores: dict[str, float] = {}
        if not papers:
            return scores
        batch_size = max(1, int(self.cfg.get("llm.relevance_batch_size", 8)))
        for start in range(0, len(papers), batch_size):
            batch = papers[start : start + batch_size]
            payload = "\n\n".join(
                f"[id={idx}]\n标题: {p.title}\n发表处: {p.venue or '未知'}\n摘要: "
                f"{textutil.truncate(p.abstract, 500) or '（无摘要）'}"
                for idx, p in enumerate(batch, start=1)
            )
            result = self.complete(
                "relevance_system",
                "relevance_user",
                "relevance",
                direction=direction or "（未指定）",
                keywords=", ".join(keywords or []) or "（未指定）",
                papers=payload,
            )
            rows = []
            if isinstance(result.data, dict):
                rows = result.data.get("scores") or []
            for row in rows:
                try:
                    idx = int(row.get("id"))
                    value = float(row.get("score"))
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(batch):
                    scores[batch[idx - 1].key] = max(0.0, min(1.0, value / 10.0))
        return scores


# --------------------------------------------------------------------------- #
# 启发式兜底（无 LLM 时使用）
# --------------------------------------------------------------------------- #


def _paper_payload(idx: int, paper) -> str:
    authors = ", ".join(paper.authors[:4]) + (" 等" if len(paper.authors) > 4 else "")
    abstract = textutil.truncate(paper.abstract, 900) or "（无摘要，请依据标题推断）"
    return (
        f"[id={idx}]\n标题: {paper.title}\n作者: {authors or '未知'}\n"
        f"年份: {paper.year or '未知'}\n发表处: {paper.venue or '未知'}\n摘要: {abstract}"
    )


def _heuristic_card(paper, direction: str, keywords: list[str]) -> PaperCard:
    sentences = textutil.split_sentences(paper.abstract)
    summary = " ".join(sentences[:2]) if sentences else f"《{paper.title}》（{paper.year or '年份未知'}）"
    summary = textutil.truncate(summary, 220)

    venue = paper.venue or "发表处未标注"
    highlights: list[str] = []
    if paper.venue:
        highlights.append(f"发表于 {paper.venue}")
    if paper.citations:
        highlights.append(f"被引 {paper.citations} 次")
    if paper.item_type == "preprint":
        highlights.append("预印本，抢先看最新进展")
    hits = [k for k in (keywords or []) if k.lower() in f"{paper.title} {paper.abstract}".lower()]
    if hits:
        highlights.append("命中关键词：" + "、".join(hits[:4]))
    if not highlights:
        highlights = ["标题与当前方向存在潜在关联，建议人工确认"]

    topic_hint = textutil.truncate(direction, 40) or "当前方向"
    if hits:
        reason = (
            f"命中方向关键词 {', '.join(hits[:5])}，与「{topic_hint}」直接相关，"
            "建议优先确认它是否解决了你关心的那类问题。"
        )
    else:
        reason = f"未命中显式关键词，但与 {venue} 上的工作同处一个子领域，可作为背景参考。"
    return PaperCard(summary=summary, highlights=highlights, reason=reason)


def heuristic_profile(direction: str, seed_texts: list[str], *, max_keywords: int = 20, max_queries: int = 6, stopwords=None) -> dict:
    """没有 LLM 时的方向解析：词频 + 二元短语 → 关键词，再拼成检索式。"""
    texts = [direction or ""] + list(seed_texts)
    keywords = textutil.extract_keywords(texts, stopwords=stopwords, top_k=max_keywords)
    queries: list[str] = []
    for i in range(0, len(keywords), 2):
        pair = keywords[i : i + 2]
        if not pair:
            break
        if len(pair) == 1:
            queries.append(f'"{pair[0]}"')
        else:
            queries.append(f'"{pair[0]}" AND "{pair[1]}"')
        if len(queries) >= max_queries:
            break
    if not queries and direction.strip():
        queries = [direction.strip()]
    return {
        "profile_summary": textutil.truncate(re.sub(r"\s+", " ", direction or " ".join(seed_texts)), 200),
        "keywords": keywords,
        "queries": queries,
        "negative_keywords": [],
        "provider": "heuristic",
        "error": "",
    }
