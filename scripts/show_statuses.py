"""查看某个方向下各状态的推荐数量与待推送清单（核对"没发出去"的论文有没有被退回）。"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_radar.config import get_config  # noqa: E402
from paper_radar.store import Store  # noqa: E402

cfg = get_config()
store = Store(cfg.db_path())
for topic in store.list_topics():
    recs = store.list_recommendations(topic.id, limit=200)
    counts = Counter(r["status"] for r in recs)
    print(f"[{topic.id}] {topic.name}  共 {len(recs)} 条  状态分布: {dict(counts)}")
    pending = [r for r in recs if r["status"] == "pending"]
    for r in pending:
        score = round((r["score"] or 0) * 100)
        title = (r["title"] or "(无标题)")[:64]
        print(f"     待推送 {score:>3}  {title}")
store.close()
