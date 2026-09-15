"""验证 Zotero 全链路：读分类 → 写入 → 连点幂等 → 回滚删除（不留垃圾）。

用法：python scripts/check_zotero.py [--keep]
      --keep 表示保留写入的条目（默认写完就删，只验证权限与字段映射）

本地库用临时目录，所以不会污染你的 data/paper_radar.db。
"""

from __future__ import annotations

import sys
import tempfile

from paper_radar.config import get_config
from paper_radar.engine import Context, Engine
from paper_radar.models import Paper

KEEP = "--keep" in sys.argv


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = get_config()
        cfg.set("app.data_dir", tmp)          # 所有本地写入都落在临时目录
        ctx = Context(cfg)
        engine = Engine(ctx)
        zot = ctx.zotero

        print("状态:", zot.status())
        if not zot.enabled():
            print("Zotero 未配置，跳过。")
            return

        print("userID:", zot.user_id())
        collections = zot.list_collections(refresh=True)
        print(f"分类 {len(collections)} 个：", [c["name"] for c in collections][:12])

        paper = Paper(
            title="[paper-radar 写入自检] Buffer Management Connectivity Probe",
            authors=["Paper Radar"],
            year=2026,
            venue="paper-radar selftest",
            doi="10.0000/paper-radar-selftest",
            url="https://github.com/blueroaring/paper-radar",
            abstract="这是一条由 paper-radar 自检脚本写入的临时条目，用于验证 Zotero Web API 写入链路。",
            item_type="conferencePaper",
            citations=0,
            source="selftest",
            sources=["selftest"],
        )
        ctx.store.upsert_papers([paper])
        print("远端查重（应为 None）:", zot.find_duplicate(paper))

        first = engine.add_to_zotero([paper.key], tags=["paper-radar", "selftest"])
        print("第一次入库:", {k: v for k, v in first.items() if k != "items"})
        for item in first["items"]:
            print("   -", item)

        second = engine.add_to_zotero([paper.key], tags=["paper-radar", "selftest"])
        print("第二次入库（本地幂等，应全部 duplicate）:",
              {k: v for k, v in second.items() if k != "items"})

        added = [i for i in first["items"] if i.get("status") == "added"]
        if not added:
            print("没有成功写入，无法继续验证。")
            return
        print("写入后远端查重:", zot.find_duplicate(paper), "（Zotero 搜索索引有延迟，可能仍为 None，属正常）")

        if KEEP:
            print("--keep：保留该条目，请自行到 Zotero 里删除。")
            return
        print("回滚删除:", zot.delete_items(added))
        ctx.store.close()   # Windows 上必须先关库，临时目录才能删除


if __name__ == "__main__":
    main()
