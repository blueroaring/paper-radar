"""命令行入口：

    python -m paper_radar serve                  # 启动可视化控制台（含每日定时）
    python -m paper_radar daemon                 # 只跑定时器，不开网页
    python -m paper_radar topic add --name X --direction "..."
    python -m paper_radar build 1                # 生成/刷新方向画像
    python -m paper_radar search --topic 1       # 命令行检索
    python -m paper_radar digest --topic 1       # 立即跑一次简报（默认不发信，加 --send 发信）
    python -m paper_radar sources --test         # 数据源连通性体检
    python -m paper_radar selftest               # 检查 LLM / Zotero / 邮件是否配好
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .config import get_config
from .digest import run_digest
from .engine import Context, Engine, default_topic_from_payload
from .sources import available_sources, build_sources
from .store import now_iso
from .textutil import truncate


def _print_table(papers: list) -> None:
    if not papers:
        print("（没有结果）")
        return
    for idx, p in enumerate(papers, start=1):
        data = p if isinstance(p, dict) else p.to_dict()
        print(f"\n{idx}. {data.get('title')}")
        meta = [str(data.get("year") or "年份未知"), data.get("venue") or "发表处未标注"]
        if data.get("citations") is not None:
            meta.append(f"被引 {data['citations']}")
        score = data.get("score")
        if score is not None:
            meta.append(f"相关度 {round(float(score) * 100)}")
        print("   " + " · ".join(meta))
        if data.get("summary"):
            print("   概要: " + truncate(data["summary"], 200))
        for h in data.get("highlights") or []:
            print("   特色: " + h)
        if data.get("reason"):
            print("   理由: " + truncate(data["reason"], 200))
        link = data.get("url") or (f"https://doi.org/{data['doi']}" if data.get("doi") else "")
        if link:
            print("   链接: " + link)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paper_radar", description="文献检索推荐 + 每日简报")
    parser.add_argument("--version", action="version", version=f"paper-radar {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动 Web 控制台")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-scheduler", action="store_true", help="不启动内置定时器")

    sub.add_parser("daemon", help="只运行每日定时器")

    p_topic = sub.add_parser("topic", help="管理研究方向")
    topic_sub = p_topic.add_subparsers(dest="topic_command")
    topic_sub.add_parser("list", help="列出方向")
    p_add = topic_sub.add_parser("add", help="新增方向")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--direction", default="")
    p_add.add_argument("--seed", action="append", default=[], help="种子论文，可重复")
    p_add.add_argument("--keywords", default="", help="逗号分隔")
    p_add.add_argument("--queries", default="", help="逗号分隔")
    topic_sub.add_parser("show", help="查看方向").add_argument("id", type=int)
    topic_sub.add_parser("delete", help="删除方向").add_argument("id", type=int)

    p_build = sub.add_parser("build", help="生成方向画像（关键词 + 检索式）")
    p_build.add_argument("id", type=int)

    p_search = sub.add_parser("search", help="执行一次检索")
    p_search.add_argument("--topic", type=int, default=None)
    p_search.add_argument("--name", default="")
    p_search.add_argument("--direction", default="")
    p_search.add_argument("--seed", action="append", default=[])
    p_search.add_argument("--limit", type=int, default=None)
    p_search.add_argument("--source", action="append", default=[])
    p_search.add_argument("--no-llm", action="store_true")
    p_search.add_argument("--json", action="store_true")

    p_digest = sub.add_parser("digest", help="立即执行一次每日简报")
    p_digest.add_argument("--topic", type=int, action="append", default=[])
    p_digest.add_argument("--top", type=int, default=None)
    p_digest.add_argument("--send", action="store_true", help="真的发邮件（默认只生成报告）")
    p_digest.add_argument("--json", action="store_true")

    p_sources = sub.add_parser("sources", help="列出/测试数据源")
    p_sources.add_argument("--test", action="store_true")
    p_sources.add_argument("--query", default="buffer management switch")

    p_self = sub.add_parser("selftest", help="检查 LLM / Zotero / 邮件配置")
    p_self.add_argument("--mail", action="store_true", help="真的发一封测试邮件")

    args = parser.parse_args(argv)
    ctx = Context(get_config())
    engine = Engine(ctx)

    if args.command in (None, "serve"):
        from .server import serve_forever

        if args.command == "serve" and args.port:
            ctx.cfg.set("app.port", args.port)
        serve_forever(ctx, with_scheduler=not getattr(args, "no_scheduler", False))
        return 0

    if args.command == "daemon":
        from .scheduler import DailyScheduler

        sched = DailyScheduler(ctx)
        sched.start()
        print("Paper Radar 定时器已启动：", json.dumps(sched.status(), ensure_ascii=False))
        print("按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(30)
        except KeyboardInterrupt:
            sched.stop()
        return 0

    if args.command == "topic":
        if args.topic_command in (None, "list"):
            for topic in ctx.store.list_topics():
                flag = "启用" if topic.enabled else "暂停"
                print(f"[{topic.id}] {topic.name}（{flag}）关键词 {len(topic.keywords)} 检索式 {len(topic.queries)}")
                if topic.direction:
                    print("     " + truncate(topic.direction, 110))
            return 0
        if args.topic_command == "add":
            topic = default_topic_from_payload(
                {
                    "name": args.name,
                    "direction": args.direction,
                    "seeds": args.seed,
                    "keywords": [k.strip() for k in args.keywords.split(",") if k.strip()],
                    "queries": [q.strip() for q in args.queries.split(",") if q.strip()],
                }
            )
            topic.id = ctx.store.save_topic(topic)
            print(f"已创建方向 #{topic.id} {topic.name}")
            if not topic.queries:
                print("提示：接着运行 `python -m paper_radar build %d` 生成检索式。" % topic.id)
            return 0
        if args.topic_command == "show":
            topic = ctx.store.get_topic(args.id)
            if not topic:
                print("方向不存在"); return 1
            print(json.dumps(topic.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.topic_command == "delete":
            ctx.store.delete_topic(args.id)
            print(f"已删除方向 #{args.id}")
            return 0

    if args.command == "build":
        topic = ctx.store.get_topic(args.id)
        if not topic:
            print("方向不存在"); return 1
        engine.build_profile(topic, progress=lambda m: print(" · " + m))
        print(f"关键词：{', '.join(topic.keywords)}")
        for q in topic.queries:
            print("检索式：" + q)
        return 0

    if args.command == "search":
        topic = None
        if args.topic:
            topic = ctx.store.get_topic(args.topic)
            if not topic:
                print("方向不存在"); return 1
        else:
            topic = default_topic_from_payload(
                {
                    "name": args.name or "临时检索",
                    "direction": args.direction,
                    "seeds": args.seed,
                }
            )
            if not topic.queries:
                engine.build_profile(topic, progress=lambda m: print(" · " + m))
        result = engine.search(
            topic,
            limit=args.limit,
            sources=args.source or None,
            summarize=not args.no_llm,
            record=False,
            progress=lambda m: print(" · " + m),
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_table(result["papers"])
            if result.get("errors"):
                print("\n失败的数据源：" + json.dumps(result["errors"], ensure_ascii=False))
        return 0

    if args.command == "digest":
        summary = run_digest(
            ctx,
            topic_ids=args.topic or None,
            top_n=args.top,
            send=args.send,
            progress=lambda m: print(" · " + m),
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"\n新增 {summary.get('total_new', 0)} 篇。")
            for path in summary.get("reports", []):
                print("报告：" + path)
            for mail in summary.get("mail", []):
                print(("邮件已发送：" if mail.get("ok") else "邮件失败：") + str(mail.get("error") or mail.get("to")))
        return 0

    if args.command == "sources":
        if not args.test:
            for info in available_sources(ctx.cfg):
                flag = "启用" if info["enabled"] else "停用"
                print(f"{info['name']:<18} {info['label']:<18} {flag}  {info['homepage']}")
            return 0
        fetcher = ctx.fetcher
        for plugin in build_sources(ctx.cfg, fetcher):
            print(f"→ 测试 {plugin.label} …", end=" ", flush=True)
            try:
                found = plugin.search(args.query, limit=3)
                print(f"OK，{len(found)} 条", ("例：" + found[0].title[:70]) if found else "")
            except Exception as exc:  # noqa: BLE001
                print(f"失败：{type(exc).__name__}: {exc}")
        return 0

    if args.command == "selftest":
        cfg = ctx.cfg
        print("== 配置 ==")
        print(f"LLM provider : {cfg.get('llm.provider')} / {cfg.get('llm.model')} (key {'已配置' if cfg.get('llm.api_key') else '缺失'})")
        print(f"Zotero       : {'已配置' if ctx.zotero.enabled() else '未配置'}")
        print(f"邮件         : {'已启用' if ctx.mailer.enabled() else '未启用'} 收件人 {ctx.mailer.recipients()}")
        print(f"数据源       : {', '.join(cfg.get('sources.enabled', []))}")
        print("\n== 连通性 ==")
        if cfg.get("llm.api_key"):
            r = ctx.llm.complete(
                "profile_system",
                "profile_user",
                "profile",
                direction="交换机缓冲管理",
                seeds="（无）",
                max_queries=3,
            )
            print(f"LLM          : {'OK via ' + r.provider if r.data else '失败 ' + r.error}")
        if ctx.zotero.enabled():
            try:
                uid = ctx.zotero.user_id()
                colls = ctx.zotero.list_collections(refresh=True)
                print(f"Zotero       : OK userID={uid} 分类 {len(colls)} 个")
            except Exception as exc:  # noqa: BLE001
                print(f"Zotero       : 失败 {exc}")
        if args.mail and ctx.mailer.recipients():
            res = ctx.mailer.send(
                subject=f"[Paper Radar] 自检邮件 {now_iso()}",
                html="<p>这是一封 Paper Radar 的自检邮件。</p>",
                text="这是一封 Paper Radar 的自检邮件。",
                force=True,
            )
            print(f"邮件         : {res}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
