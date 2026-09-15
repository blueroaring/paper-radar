"""每日简报：到点自动检索最新论文 → 写卡片 → 发到邮箱 → 存一份 HTML 报告。

一个方向一封邮件（主题里带方向名），多个方向就多封；
每封邮件都会同时在 data/reports/ 下留一份 HTML，方便在浏览器里复看。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import render
from .engine import Engine
from .models import Paper, Topic
from .store import now_iso
from .textutil import slugify

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


def _published_date(paper: Paper) -> datetime | None:
    raw = str(paper.extra.get("published") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_recent(papers: list[Paper], *, lookback_days: int) -> list[Paper]:
    """只保留"最近"的论文：有精确发布日期的按日期，否则按年份粗筛。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
    out = []
    for paper in papers:
        stamp = _published_date(paper)
        if stamp is not None:
            if stamp >= cutoff:
                out.append(paper)
            continue
        if paper.year is None or paper.year >= cutoff.year:
            out.append(paper)
    return out


def _subject(template: str, *, topic: Topic, count: int, date: str) -> str:
    try:
        return template.format(date=date, topic=topic.name, count=count)
    except (KeyError, IndexError):
        return f"[Paper Radar] {date} · {topic.name} · {count} 篇新论文"


def run_digest(
    ctx,
    *,
    topic_ids: list[int] | None = None,
    top_n: int | None = None,
    send: bool | None = None,
    save_report: bool | None = None,
    progress: Progress = _noop,
) -> dict:
    cfg = ctx.cfg
    engine = Engine(ctx)
    digest_cfg = cfg.get("digest", {}) or {}
    if top_n is None:
        top_n = int(digest_cfg.get("top_n", 5))
    if send is None:
        send = bool(cfg.get("mail.enabled", False))
    if save_report is None:
        save_report = bool(digest_cfg.get("save_report", True))
    only_new = bool(digest_cfg.get("only_new", True))
    lookback_days = int(digest_cfg.get("lookback_days", 45))

    if topic_ids:
        topics = [t for t in (ctx.store.get_topic(i) for i in topic_ids) if t]
    else:
        configured = list(digest_cfg.get("topics", []) or [])
        topics = [
            t
            for t in ctx.store.list_topics(only_enabled=True)
            if not configured or t.name in configured or t.id in configured
        ]

    summary: dict = {
        "started_at": now_iso(),
        "topics": [],
        "total_new": 0,
        "mail": [],
        "reports": [],
    }
    if not topics:
        summary["note"] = "没有启用的研究方向，先去「研究方向」页建一个。"
        return summary

    date_str = datetime.now().strftime("%Y-%m-%d")
    for topic in topics:
        assert topic.id is not None
        run_id = ctx.store.start_run("digest", topic.id)
        entry: dict = {"topic_id": topic.id, "topic": topic.name, "items": [], "errors": {}}
        try:
            progress(f"[{topic.name}] 检索最新论文…")
            collected = engine.collect(
                topic,
                limit=max(top_n * 4, 20),
                year_from=(datetime.now(timezone.utc) - timedelta(days=lookback_days)).year,
                progress=progress,
            )
            papers: list[Paper] = collected["papers"]
            entry["errors"] = collected.get("errors", {})
            entry["candidates"] = collected.get("candidates", 0)

            papers = filter_recent(papers, lookback_days=lookback_days)
            if only_new:
                before = len(papers)
                papers = ctx.store.filter_new(topic.id, papers)
                progress(f"[{topic.name}] 过滤已推送：{before} → {len(papers)} 篇")

            # 先语义重排（只对前若干个候选，控制成本），再按最低分阈值剔除蹭关键词的论文
            papers = engine.rerank_with_llm(topic, papers[: max(top_n * 3, 10)], progress=progress)
            min_score = float(digest_cfg.get("min_score", 0.0) or 0.0)
            if min_score:
                kept = [p for p in papers if p.score >= min_score]
                if len(kept) != len(papers):
                    progress(f"[{topic.name}] 低于相关性阈值 {min_score} 被剔除：{len(papers) - len(kept)} 篇")
                papers = kept
            papers = papers[:top_n]
            entry["new"] = len(papers)

            if not papers:
                progress(f"[{topic.name}] 没有新论文，跳过发信。")
                ctx.store.finish_run(run_id, status="empty", stats=entry)
                summary["topics"].append(entry)
                continue

            recs, provider = engine.summarize_papers(topic, papers, progress=progress)
            ctx.store.save_recommendations(
                topic.id, recs, status="sent" if send else "shown", provider=provider
            )
            items = [rec.to_dict() for rec in recs]
            entry["items"] = items
            entry["provider"] = provider

            # ---- 报告落盘 ----
            local_ui = f"http://{cfg.get('app.host', '127.0.0.1')}:{cfg.get('app.port', 8848)}"
            html = render.render_digest(
                topic_name=topic.name,
                direction=topic.profile_summary or topic.direction,
                items=items,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                sources_used=collected.get("sources"),
                local_ui_url=local_ui,
                title=f"Paper Radar · {topic.name}",
            )
            report_path = ""
            if save_report:
                # 文件名带时分：同一天手动试跑与定时运行不会互相覆盖
                stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
                path = ctx.cfg.reports_dir() / f"{stamp}-{slugify(topic.name)}.html"
                path.write_text(html, encoding="utf-8")
                report_path = str(path)
                summary["reports"].append(report_path)
                entry["report"] = report_path

            # ---- 发信 ----
            if send:
                subject = _subject(
                    cfg.get("mail.subject_template", "[Paper Radar] {date} · {topic} · {count} 篇新论文"),
                    topic=topic,
                    count=len(items),
                    date=date_str,
                )
                # 邮件里只放前 N 篇，避免正文过长（N 可在 config 调）
                limit_mail = int(cfg.get("mail.max_items_in_mail", 8) or 8)
                mail_items = items[:limit_mail]
                mail_html = render.render_digest(
                    topic_name=topic.name,
                    direction=topic.profile_summary or topic.direction,
                    items=mail_items,
                    generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    sources_used=collected.get("sources"),
                    local_ui_url=local_ui,
                    title=f"Paper Radar · {topic.name}",
                )
                result = ctx.mailer.send(
                    subject=subject,
                    html=mail_html,
                    text=render.render_text(mail_items, topic_name=topic.name),
                )
                entry["mail"] = result
                summary["mail"].append({"topic": topic.name, **result})
                progress(f"[{topic.name}] 邮件：{'已发送' if result.get('ok') else '失败 - ' + str(result.get('error'))}")

            summary["total_new"] += len(items)
            ctx.store.finish_run(run_id, status="ok", stats=entry, report_path=report_path)
            summary["topics"].append(entry)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            ctx.store.finish_run(run_id, status="error", stats=entry, error=str(exc))
            summary["topics"].append(entry)
            progress(f"[{topic.name}] 出错：{exc}")

    summary["finished_at"] = now_iso()
    return summary


def latest_report(cfg) -> Path | None:
    reports = sorted(cfg.reports_dir().glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None
