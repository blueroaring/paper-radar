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
from pathlib import Path

from . import __version__, render
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

    p_rem = sub.add_parser("remembered", help="高分记忆：查看 / 导出 / 忘记")
    p_rem.add_argument("--threshold", type=float, default=None, help="临时覆盖相关度阈值")
    p_rem.add_argument("--all", action="store_true", help="列出全部记忆（不过滤阈值）")
    p_rem.add_argument("--export", choices=["markdown", "md", "bibtex", "bib", "csv", "json"], default=None)
    p_rem.add_argument("--out", default=None, help="导出到文件（默认打印到终端）")
    p_rem.add_argument("--forget", default=None, help="按 paper key 删除一条记忆")
    p_rem.add_argument("--backfill", action="store_true", help="按阈值回填历史推荐")

    p_dedupe = sub.add_parser("dedupe", help="合并历史遗留的重复论文行（同一篇论文的不同来源指纹）")

    sub.add_parser(
        "requeue-mail",
        help="把发信失败却被标为已推送的论文退回待推送（下次运行自动重发）",
    )
    sub.add_parser("diag-mail", help="诊断邮件链路：DNS 解析 / 465 / 587 / 真实登录")

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

    if args.command == "requeue-mail":
        result = ctx.store.requeue_failed_mail()
        if not result["runs"]:
            print("没有发现「发信失败却标记为已推送」的记录，无需处理。")
            return 0
        for run in result["runs"]:
            print(f"  运行 #{run['run_id']}（{run['started_at'][:19]}）：退回 {run['keys']} 篇")
        print(f"共退回 {result['requeued']} 条为待推送 —— 下次简报运行会自动重发这几篇。")
        return 0

    if args.command == "diag-mail":
        from .diagnostics import diagnose_smtp

        return diagnose_smtp(ctx.cfg)

    if args.command == "dedupe":
        result = ctx.store.dedupe_papers()
        print(
            f"合并重复论文组 {result['groups_merged']} 个，删除冗余行 {result['rows_removed']} 条；"
            f"现有论文 {result['papers_left']} 篇、高分记忆 {result['remembered']} 条。"
        )
        if result.get("orphan_recommendations"):
            print(
                f"另有 {result['orphan_recommendations']} 条历史推荐指向已不存在的论文"
                "（早期版本缺陷，保留它们以免重复推送；不影响使用）。"
            )
        return 0

    if args.command == "remembered":
        cfg = ctx.cfg
        if args.backfill:
            threshold = args.threshold if args.threshold is not None else float(cfg.get("remember.min_score", 0.6))
            added = ctx.store.backfill_remembered(threshold)
            print(f"按相关度 ≥ {threshold} 回填：新增 {len(added)} 篇，现共 {ctx.store.remembered_count()} 篇")
            return 0
        if args.forget:
            ok = ctx.store.forget_remembered(args.forget)
            print(("已忘记：" if ok else "没有这条记忆：") + args.forget)
            return 0

        threshold = args.threshold if args.threshold is not None else float(cfg.get("remember.min_score", 0.6))
        items = ctx.store.list_remembered(limit=1000, min_score=None if args.all else threshold)

        if args.export:
            body, _ctype, ext = render.export_items(items, args.export)
            if args.out:
                Path(args.out).write_text(body, encoding="utf-8")
                print(f"已导出 {len(items)} 篇 → {args.out}")
            else:
                print(body)
            return 0

        if not items:
            print(f"高分记忆里还没有条目（当前阈值 {threshold}）。")
            print("提示：跑一次 `python -m paper_radar search --topic 1`，或 `python -m paper_radar remembered --backfill` 回填历史。")
            return 0
        print(f"高分记忆 {len(items)} 篇（{'全部' if args.all else f'相关度 ≥ {threshold}'}，共 {ctx.store.remembered_count()} 条记录）\n")
        for idx, item in enumerate(items, start=1):
            meta = [
                f"相关度 {round(item['score'] * 100)}",
                item["venue"] or "发表处未标注",
                str(item["year"] or "年份未知"),
                item["topic_name"] or "未关联方向",
                item["first_seen"][:10],
            ]
            print(f"{idx}. [★] {item['title']}")
            print("   " + " · ".join(meta))
            if item.get("summary"):
                print("   概要: " + truncate(item["summary"], 150))
            if item.get("note"):
                print("   批注: " + item["note"])
            print(f"   key: {item['key']}")
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
