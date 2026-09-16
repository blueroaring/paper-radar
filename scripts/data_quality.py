"""统计论文元数据质量（DOI / 发表处 / 摘要覆盖率），用于核对"只升不降"策略的效果。

用法：python scripts/data_quality.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "paper_radar.db"
conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)


def scalar(sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


total = scalar("SELECT COUNT(1) FROM papers")
print(f"论文总数 {total}")

print(f"  DOI 覆盖        : {scalar('SELECT COUNT(1) FROM papers WHERE doi != %s' % repr(''))} / {total}")
print(f"    - 出版社 DOI  : {scalar(chr(83) + 'ELECT COUNT(1) FROM papers WHERE doi != ? AND doi NOT LIKE ?', '', '%10.48550%')}")
print(f"    - arXiv DOI   : {scalar('SELECT COUNT(1) FROM papers WHERE doi LIKE ?', '%10.48550%')}")
print(f"  arXiv ID 覆盖   : {scalar('SELECT COUNT(1) FROM papers WHERE arxiv_id != ?', '')} / {total}")
print(f"  摘要覆盖        : {scalar('SELECT COUNT(1) FROM papers WHERE length(abstract) > 50')} / {total}")
print(f"  发表处为空      : {scalar('SELECT COUNT(1) FROM papers WHERE venue = ?', '')} / {total}")
print(f"  发表处仍是预印本: {scalar('SELECT COUNT(1) FROM papers WHERE lower(venue) LIKE ?', '%arxiv%')} / {total}")

print(f"\n记忆条目 {scalar('SELECT COUNT(1) FROM remembered')}；推荐记录 {scalar('SELECT COUNT(1) FROM recommendations')}")
orphans = scalar(
    "SELECT COUNT(1) FROM recommendations r LEFT JOIN papers p ON p.key = r.paper_key WHERE p.key IS NULL"
)
print(f"孤儿推荐（历史遗留，保留以免重复推送）：{orphans}")
dups = scalar(
    "SELECT COUNT(1) FROM (SELECT title_norm FROM papers GROUP BY title_norm HAVING COUNT(1) > 1)"
)
print(f"重复标题组：{dups}")
conn.close()
