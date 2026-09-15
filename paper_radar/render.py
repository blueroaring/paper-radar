"""HTML 渲染：推荐表 / 每日简报（邮件正文 + 本地报告页）。

邮件客户端对 CSS 支持很差，所以这里用**内联样式 + table 布局**，
同时保留一份纯文本版本给不支持 HTML 的收件端。
"""

from __future__ import annotations

from typing import Any

from .textutil import esc, truncate

ROW_FIELDS = ("title", "venue", "year", "summary", "highlights", "reason")


def _get(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def normalize_item(item: Any) -> dict:
    """把 Recommendation / dict / Paper 统一成渲染用的扁平结构。"""
    paper = _get(item, "paper", None)
    data = {
        "key": _get(item, "key", "") or (_get(paper, "key", "") if paper else ""),
        "title": _get(item, "title", "") or (_get(paper, "title", "") if paper else ""),
        "venue": _get(item, "venue", "") or (_get(paper, "venue", "") if paper else ""),
        "year": _get(item, "year", "") or (_get(paper, "year", "") if paper else ""),
        "url": _get(item, "url", "") or (_get(paper, "url", "") if paper else ""),
        "doi": _get(item, "doi", "") or (_get(paper, "doi", "") if paper else ""),
        "arxiv_id": _get(item, "arxiv_id", "") or (_get(paper, "arxiv_id", "") if paper else ""),
        "citations": _get(item, "citations", None) if not paper else _get(paper, "citations", None),
        "score": _get(item, "score", 0.0),
        "summary": _get(item, "summary", ""),
        "highlights": _get(item, "highlights", []) or [],
        "reason": _get(item, "reason", ""),
        "sources": _get(item, "sources", []) or [],
        "venue_tier": (_get(item, "extra", {}) or {}).get("venue_tier", ""),
    }
    if paper:
        data["sources"] = _get(paper, "sources", []) or data["sources"]
        data["venue_tier"] = (_get(paper, "extra", {}) or {}).get("venue_tier", "")
    if isinstance(data["highlights"], str):
        data["highlights"] = [data["highlights"]]
    if data["year"] is None:
        data["year"] = ""
    return data


def _link(item: dict) -> str:
    url = item.get("url") or (f"https://doi.org/{item['doi']}" if item.get("doi") else "")
    if not url and item.get("arxiv_id"):
        url = f"https://arxiv.org/abs/{item['arxiv_id']}"
    return url or ""


def _stars(score: float) -> str:
    if not score:
        return "—"
    return f"{round(float(score) * 100)}"


def render_items_table(items: list[Any], *, local_ui_url: str = "") -> str:
    """推荐表：**内容概要 / 文章特色 / 发表在哪里** 三列是核心。"""
    head = (
        "<tr>"
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">#</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">论文</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">发表在哪里</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">内容概要</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">文章特色</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">推荐理由</th>'
        '<th style="padding:10px;border-bottom:2px solid #e2e8f0;text-align:left;font-size:13px;color:#475569;">相关度</th>'
        "</tr>"
    )
    rows = []
    for idx, raw in enumerate(items, start=1):
        item = normalize_item(raw)
        url = _link(item)
        title_html = esc(item["title"])
        if url:
            title_html = f'<a href="{esc(url)}" style="color:#1d4ed8;text-decoration:none;">{title_html}</a>'
        venue = item["venue"] or "未标注"
        tier = f' <span style="color:#b45309;font-size:11px;">[{esc(item["venue_tier"])}]</span>' if item.get("venue_tier") and item["venue_tier"] != "unknown" else ""
        highlights = "".join(
            f'<div style="margin:2px 0;">• {esc(h)}</div>' for h in item["highlights"][:4]
        ) or '<span style="color:#94a3b8;">—</span>'
        meta_bits = [str(item["year"]) if item["year"] else "年份未知"]
        if item.get("citations") is not None:
            meta_bits.append(f'被引 {item["citations"]}')
        if item.get("sources"):
            meta_bits.append("/".join(item["sources"][:3]))
        rows.append(
            "<tr>"
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;color:#94a3b8;font-size:12px;">{idx}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:13px;line-height:1.5;">'
            f'<div style="font-weight:600;color:#0f172a;">{title_html}</div>'
            f'<div style="color:#94a3b8;font-size:11px;margin-top:3px;">{" · ".join(esc(str(b)) for b in meta_bits)}</div></td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:12px;color:#334155;">{esc(venue)}{tier}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:12px;color:#334155;line-height:1.6;">{esc(item["summary"]) or "—"}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:12px;color:#334155;line-height:1.6;">{highlights}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:12px;color:#334155;line-height:1.6;">{esc(item["reason"]) or "—"}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:12px;color:#0f766e;font-weight:600;">{_stars(item["score"])}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f"<thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"
    )


def render_digest(
    *,
    topic_name: str,
    direction: str,
    items: list[Any],
    generated_at: str,
    sources_used: list[str] | None = None,
    local_ui_url: str = "",
    title: str = "Paper Radar 每日简报",
) -> str:
    table = render_items_table(items, local_ui_url=local_ui_url)
    src_line = "、".join(sources_used or [])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:24px;background:#f6f8fb;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<div style="max-width:1180px;margin:0 auto;background:#ffffff;border-radius:14px;padding:26px 26px 18px;box-shadow:0 2px 12px rgba(15,23,42,.06);">
  <div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#64748b;">Paper Radar</div>
  <h1 style="margin:8px 0 4px;font-size:21px;">{esc(topic_name)}</h1>
  <div style="color:#64748b;font-size:13px;line-height:1.7;">{esc(truncate(direction, 320))}</div>
  <div style="margin:14px 0 18px;color:#94a3b8;font-size:12px;">
    生成时间 {esc(generated_at)} · 共 {len(items)} 篇 · 检索源 {esc(src_line or '—')}
  </div>
  {table}
  <div style="margin-top:18px;color:#94a3b8;font-size:12px;line-height:1.7;">
    想看全文或直接入库？本机打开 <a href="{esc(local_ui_url or 'http://127.0.0.1:8848')}" style="color:#1d4ed8;">Paper Radar 控制台</a>，
    在「检索推荐」里勾选后一键加入 Zotero。
  </div>
</div>
</body></html>"""


def render_text(items: list[Any], *, topic_name: str = "") -> str:
    lines = [f"Paper Radar · {topic_name}".strip(), ""]
    for idx, raw in enumerate(items, start=1):
        item = normalize_item(raw)
        lines.append(f"{idx}. {item['title']} ({item['year'] or '年份未知'})")
        lines.append(f"   发表处: {item['venue'] or '未标注'}   相关度: {_stars(item['score'])}")
        lines.append(f"   概要: {item['summary'] or '—'}")
        if item["highlights"]:
            lines.append("   特色: " + "；".join(item["highlights"]))
        lines.append(f"   推荐理由: {item['reason'] or '—'}")
        link = _link(item)
        if link:
            lines.append(f"   链接: {link}")
        lines.append("")
    return "\n".join(lines)


def render_overview(topics: list[dict], runs: list[dict], stats: dict) -> str:
    """给本地控制台用的概览片段（保持纯 HTML，无 JS 依赖）。"""
    topic_rows = "".join(
        f'<li><b>{esc(t.get("name", ""))}</b> — {esc(truncate(t.get("direction", ""), 90))}'
        f' <span style="color:#94a3b8;">({len(t.get("queries") or [])} 条检索式)</span></li>'
        for t in topics
    ) or "<li>还没有研究方向，去「研究方向」页新建一个。</li>"
    run_rows = "".join(
        f'<li>{esc(r.get("started_at", ""))} · {esc(r.get("kind", ""))} · {esc(r.get("status", ""))}'
        f' {esc(str(r.get("stats", {})))}</li>'
        for r in runs[:8]
    ) or "<li>暂无运行记录。</li>"
    return (
        f'<p>论文目录 {stats.get("papers", 0)} 篇 · 主题 {stats.get("topics", 0)} 个 · '
        f'推荐 {stats.get("recommendations", 0)} 条 · 已入库 {stats.get("accepted", 0)} 条</p>'
        f"<h3>研究方向</h3><ul>{topic_rows}</ul>"
        f"<h3>最近运行</h3><ul>{run_rows}</ul>"
    )
