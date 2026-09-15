"""端到端自检脚本（不属于核心代码，放在 scripts/ 作为示例）。"""

from __future__ import annotations

import json
import sys

from paper_radar.config import get_config
from paper_radar.engine import Context, Engine, default_topic_from_payload

STEP = sys.argv[1] if len(sys.argv) > 1 else "all"


def main() -> None:
    ctx = Context(get_config())
    engine = Engine(ctx)

    if STEP in ("all", "topic"):
        existing = ctx.store.find_topic_by_name("交换机缓冲管理")
        topic = default_topic_from_payload(
            {
                "id": existing.id if existing else None,
                "name": "交换机缓冲管理",
                "direction": (
                    "我关注数据中心与片上交换机的共享缓存/缓冲管理（buffer management）："
                    "动态阈值与占用感知的缓冲分配、抢占式缓冲（preemptive buffer management）、"
                    "HBM 与片上 SRAM 的混合缓存体系、以及缓冲管理对丢包率、时延尾部分布和吞吐的影响。"
                ),
                "seeds": [
                    "Themis: Scheduling-Aware Buffer Management for HBM-Based Hybrid Buffers",
                    "10.1145/3789240.3829188",
                    "Occamy: A Preemptive Buffer Management for On-chip Shared-memory Switches",
                ],
                "zotero_collection": "5KNWQAGJ",
            }
        )
        topic.id = ctx.store.save_topic(topic)
        print(f"[topic] id={topic.id} name={topic.name}")
        engine.build_profile(topic, progress=lambda m: print("  · " + m))
        print("[keywords] " + ", ".join(topic.keywords))
        for q in topic.queries:
            print("[query] " + q)
        print("[summary] " + topic.profile_summary)
        print("[seeds]")
        for seed in topic.seeds:
            print(f"   {seed.value[:50]!r} -> {seed.title[:70]!r} doi={seed.doi} arxiv={seed.arxiv_id}")

    if STEP in ("all", "search"):
        topic = ctx.store.find_topic_by_name("交换机缓冲管理")
        assert topic
        result = engine.search(topic, limit=12, summarize=True, record=True, progress=lambda m: print("  · " + m))
        print("\n[candidates]", result["candidates"], "| errors:", json.dumps(result["errors"], ensure_ascii=False))
        print("[provider]", result["provider"])
        for idx, paper in enumerate(result["papers"], 1):
            print(f"\n{idx}. {paper['title']}")
            print(f"   {paper['year']} · {paper['venue'] or '未标注'} · 相关度 {round(paper['score']*100)} · {paper['sources']}")
            print(f"   概要: {paper['summary'][:160]}")
            print(f"   特色: {' / '.join(paper['highlights'][:3])}")
            print(f"   理由: {paper['reason'][:160]}")
        print("\n[top keys]", [p["key"] for p in result["papers"][:3]])
        with open("data/_last_search.json", "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
