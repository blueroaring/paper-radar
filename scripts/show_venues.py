"""查看论文/记忆的发表处，用于核对「正式会议名优先」是否生效。

用法：python scripts/show_venues.py [关键词]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "paper_radar.db"
needle = sys.argv[1] if len(sys.argv) > 1 else ""

conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print(f"=== papers 表（关键词：{needle or '全部'}）===")
sql = "SELECT key, title, venue, year, citations, doi FROM papers ORDER BY title"
for row in conn.execute(sql):
    if needle and needle.lower() not in (row["title"] or "").lower():
        continue
    print(f"  {row['venue'] or '(空)':<58} | {row['year'] or '????'} | 被引 {row['citations'] or 0:>4} | {row['doi'] or '(无 DOI)'}")
    print(f"      {row['title'][:88]}")

print("\n=== 高分记忆里的发表处 ===")
for row in conn.execute(
    "SELECT m.score, p.venue, p.title FROM remembered m JOIN papers p ON p.key = m.paper_key ORDER BY m.score DESC"
):
    print(f"  {round(row['score'] * 100):>3}  {row['venue'] or '(空)':<58} | {row['title'][:52]}")

print("\n=== 看起来仍是预印本占位名的论文（应尽量少）===")
placeholders = [r for r in conn.execute("SELECT title, venue FROM papers") if "arxiv" in (r["venue"] or "").lower()]
print(f"  共 {len(placeholders)} 篇")
for row in placeholders[:8]:
    print(f"    {row['venue']:<46} | {row['title'][:56]}")
conn.close()
