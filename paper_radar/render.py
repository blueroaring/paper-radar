"""HTML 渲染：推荐表 / 每日简报（邮件正文 + 本地报告页）。

邮件客户端对 CSS 支持很差，所以这里用**内联样式 + table 布局**，
同时保留一份纯文本版本给不支持 HTML 的收件端。
"""

from __future__ import annotations

import re
from typing import Any

from .textutil import esc, truncate

ROW_FIELDS = ("title", "venue", "year", "summary", "highlights", "reason")


def _get(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def normalize_item(item: Any) -> dict:
    """把 Recommendation / dict / Paper 统一成渲染与导出用的扁平结构。

    注意：这里**必须携带所有下游要用的字段**（作者、条目类型、批注、方向、记住时间…），
    否则导出 BibTeX 会丢作者、CSV 会缺列 —— 这类"少搬一个字段"的 bug 很隐蔽，
    加字段时请同步 exporters / UI 的用法。
    """
    paper = _get(item, "paper", None)

    def pick(name: str, fallback: Any = "") -> Any:
        value = _get(item, name, None)
        if value in (None, "") and paper is not None:
            value = _get(paper, name, None)
        return fallback if value in (None, "") else value

    data = {
        "key": pick("key"),
        "title": pick("title"),
        "authors": pick("authors", []),
        "year": pick("year"),
        "venue": pick("venue"),
        "doi": pick("doi"),
        "arxiv_id": pick("arxiv_id"),
        "url": pick("url"),
        "abstract": pick("abstract"),
        "citations": pick("citations", None),
        "item_type": pick("item_type", "journalArticle"),
        "sources": pick("sources", []),
        "extra": pick("extra", {}),
        # 卡片三列
        "score": _get(item, "score", 0.0),
        "summary": _get(item, "summary", ""),
        "highlights": _get(item, "highlights", []) or [],
        "reason": _get(item, "reason", ""),
        # 高分记忆相关的元数据
        "remembered": bool(_get(item, "remembered", False)),
        "note": _get(item, "note", ""),
        "tags": _get(item, "tags", []) or [],
        "topic_name": _get(item, "topic_name", ""),
        "origin": _get(item, "origin", ""),
        "first_seen": _get(item, "first_seen", ""),
        "updated_at": _get(item, "updated_at", ""),
        "zotero_key": _get(item, "zotero_key", ""),
    }
    data["venue_tier"] = (data["extra"] or {}).get("venue_tier", "")
    if not isinstance(data["authors"], list):
        data["authors"] = [str(data["authors"])]
    if isinstance(data["highlights"], str):
        data["highlights"] = [data["highlights"]]
    if isinstance(data["tags"], str):
        data["tags"] = [data["tags"]]
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
        # 高分记忆标记：让收件人一眼看出哪些是"系统替你记住的"高分论文
        star = (
            '<span style="display:inline-block;margin-left:6px;padding:1px 6px;border-radius:999px;'
            'background:#fef3c7;color:#92400e;font-size:10.5px;vertical-align:middle;">★ 高分记忆</span>'
            if item.get("remembered")
            else ""
        )
        rows.append(
            "<tr>"
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;color:#94a3b8;font-size:12px;">{idx}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #eef2f7;font-size:13px;line-height:1.5;">'
            f'<div style="font-weight:600;color:#0f172a;">{title_html}{star}</div>'
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
    high = sum(1 for raw in items if normalize_item(raw).get("remembered"))
    high_note = (
        f' · <span style="color:#92400e;">★ 高分记忆 {high} 篇</span>（相关度达到阈值，已自动记入控制台的「高分记忆」页）'
        if high
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:24px;background:#f6f8fb;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<div style="max-width:1180px;margin:0 auto;background:#ffffff;border-radius:14px;padding:26px 26px 18px;box-shadow:0 2px 12px rgba(15,23,42,.06);">
  <div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#64748b;">Paper Radar</div>
  <h1 style="margin:8px 0 4px;font-size:21px;">{esc(topic_name)}</h1>
  <div style="color:#64748b;font-size:13px;line-height:1.7;">{esc(truncate(direction, 320))}</div>
  <div style="margin:14px 0 18px;color:#94a3b8;font-size:12px;">
    生成时间 {esc(generated_at)} · 共 {len(items)} 篇 · 检索源 {esc(src_line or '—')}{high_note}
  </div>
  {table}
  <div style="margin-top:18px;color:#94a3b8;font-size:12px;line-height:1.7;">
    想看全文或直接入库？本机打开 <a href="{esc(local_ui_url or 'http://127.0.0.1:8848')}" style="color:#1d4ed8;">Paper Radar 控制台</a>，
    在「检索推荐」里勾选后一键加入 Zotero；标 ★ 的论文已进「高分记忆」，可批量导出。
  </div>
</div>
</body></html>"""


def render_text(items: list[Any], *, topic_name: str = "") -> str:
    lines = [f"Paper Radar · {topic_name}".strip(), ""]
    for idx, raw in enumerate(items, start=1):
        item = normalize_item(raw)
        star = "  ★高分记忆" if item.get("remembered") else ""
        lines.append(f"{idx}. {item['title']} ({item['year'] or '年份未知'}){star}")
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
        f'推荐 {stats.get("recommendations", 0)} 条 · 已入库 {stats.get("accepted", 0)} 条 · '
        f'高分记忆 {stats.get("remembered", 0)} 篇</p>'
        f"<h3>研究方向</h3><ul>{topic_rows}</ul>"
        f"<h3>最近运行</h3><ul>{run_rows}</ul>"
    )


# --------------------------------------------------------------------------- #
# 导出格式（高分记忆用）
# --------------------------------------------------------------------------- #


def to_markdown(items: list[Any], *, title: str = "Paper Radar 高分记忆") -> str:
    """Markdown 清单：适合贴进 Obsidian / Notion / 周报。"""
    lines = [f"# {title}", "", f"共 {len(items)} 篇，按相关度降序。", ""]
    for idx, raw in enumerate(items, start=1):
        item = normalize_item(raw)
        link = _link(item)
        head = f"## {idx}. {item['title']}"
        if link:
            head += f"\n\n[{link}]({link})"
        lines.append(head)
        meta = [
            f"**相关度** {_stars(item['score'])}",
            f"**发表处** {item['venue'] or '未标注'}",
            f"**年份** {item['year'] or '未知'}",
        ]
        if item.get("topic_name"):
            meta.append(f"**方向** {item['topic_name']}")
        if item.get("remembered_at") or item.get("first_seen"):
            meta.append(f"**记住于** {(item.get('first_seen') or '')[:10]}")
        lines.append("- " + " · ".join(str(m) for m in meta))
        if item.get("summary"):
            lines.append(f"- **内容概要**：{item['summary']}")
        if item["highlights"]:
            lines.append("- **文章特色**：" + "；".join(item["highlights"]))
        if item.get("reason"):
            lines.append(f"- **推荐理由**：{item['reason']}")
        if item.get("note"):
            lines.append(f"- **我的批注**：{item['note']}")
        lines.append("")
    return "\n".join(lines)


_BIB_TYPES = {
    "conferencePaper": "inproceedings",
    "journalArticle": "article",
    "preprint": "misc",
    "bookSection": "incollection",
}


def _bib_key(item: dict) -> str:
    first_author = (item.get("authors") or ["anon"])[0]
    surname = first_author.split()[-1] if first_author.split() else "anon"
    surname = re.sub(r"[^A-Za-z\u4e00-\u9fff]", "", surname) or "anon"
    year = str(item.get("year") or "n.d.")
    word = re.sub(r"[^A-Za-z0-9]", "", (item["title"].split() or ["paper"])[0]) or "paper"
    return f"{surname}{year}{word}"


def to_bibtex(items: list[Any]) -> str:
    """BibTeX：可以直接 `\\input` 进 LaTeX，或拖进 Zotero / EndNote 导入。"""
    from .models import venue_quality

    chunks = []
    for raw in items:
        item = normalize_item(raw)
        entry_type = _BIB_TYPES.get(item.get("item_type", ""), "article")
        # 记录来自 arXiv（item_type=preprint），但发表处已经是正式会议/期刊时，
        # 说明这篇已经正式发表 —— 直接按会议论文导出，别给用户一条 @misc。
        if entry_type == "misc" and venue_quality(item.get("venue", "")) == 2:
            entry_type = "inproceedings"
        fields: list[tuple[str, str]] = [("title", "{" + item["title"] + "}")]
        if item.get("authors"):
            fields.append(("author", " and ".join(item["authors"])))
        if item.get("year"):
            fields.append(("year", str(item["year"])))
        if item.get("venue"):
            fields.append(("booktitle" if entry_type == "inproceedings" else "journal", item["venue"]))
        if item.get("doi"):
            fields.append(("doi", item["doi"]))
        if item.get("url"):
            fields.append(("url", item["url"]))
        # note 只能出现一次：把批注和相关度合并进同一个字段
        note_bits = [item.get("note", "")]
        if item.get("score"):
            note_bits.append(f"paper-radar relevance {_stars(item['score'])}")
        note = "; ".join(bit for bit in note_bits if bit)
        if note:
            fields.append(("note", note))
        body = ",\n  ".join(f"{name} = {{{value}}}" for name, value in fields)
        chunks.append(f"@{entry_type}{{{_bib_key(item)},\n  {body}\n}}")
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def to_csv(items: list[Any]) -> str:
    """CSV：给 Excel / pandas 用。字段顺序固定，方便写脚本。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["key", "title", "authors", "year", "venue", "doi", "url", "score", "topic", "remembered_at", "summary", "reason", "note"]
    )
    for raw in items:
        item = normalize_item(raw)
        writer.writerow(
            [
                item.get("key", ""),
                item["title"],
                "; ".join(item.get("authors") or []),
                item.get("year") or "",
                item["venue"],
                item.get("doi", ""),
                _link(item),
                _stars(item["score"]),
                item.get("topic_name", ""),
                (item.get("first_seen") or "")[:10],
                item.get("summary", ""),
                item.get("reason", ""),
                item.get("note", ""),
            ]
        )
    return buffer.getvalue()


EXPORTERS = {
    "markdown": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "md": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "bibtex": (to_bibtex, "application/x-bibtex; charset=utf-8", "bib"),
    "bib": (to_bibtex, "application/x-bibtex; charset=utf-8", "bib"),
    "csv": (to_csv, "text/csv; charset=utf-8", "csv"),
    "json": (None, "application/json; charset=utf-8", "json"),
}


def export_items(items: list[Any], fmt: str) -> tuple[str, str, str]:
    """统一导出入口，返回 (正文, content-type, 文件后缀)。新增格式只需注册进 EXPORTERS。"""
    key = (fmt or "markdown").lower()
    if key not in EXPORTERS:
        raise ValueError(f"不支持的导出格式：{fmt}（可选：{', '.join(sorted(set(EXPORTERS)))}）")
    func, ctype, ext = EXPORTERS[key]
    if func is None:  # json 直接由调用方 json.dumps
        import json

        return json.dumps(items, ensure_ascii=False, indent=2), ctype, ext
    return func(items), ctype, ext
