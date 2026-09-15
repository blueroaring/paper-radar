"""本地 Web 控制台：标准库 http.server + JSON API + 静态页面（无前端构建步骤）。

设计要点：
  * 只监听 127.0.0.1（可在 config 改），可选 app.token 做一次简单的访问校验；
  * 耗时操作（检索 / 生成方向画像 / 发简报）一律走 JobManager 异步 + 轮询进度；
  * 页面是本目录下 web/ 的纯静态文件，改完刷新即可，不需要 npm/webpack。
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import get_config, save_secrets
from .digest import run_digest
from .engine import Context, Engine, default_topic_from_payload
from .jobs import JobManager
from .models import Topic
from .scheduler import DailyScheduler
from .sources import available_sources

WEB_DIR = Path(__file__).resolve().parent / "web"


class AppState:
    """进程内共享状态：Context + 任务队列 + 定时器。"""

    def __init__(self, ctx: Context | None = None, *, with_scheduler: bool = True):
        self.ctx = ctx or Context()
        self.jobs = JobManager()
        self.scheduler: DailyScheduler | None = None
        self.with_scheduler = with_scheduler
        self.lock = threading.Lock()

    def start(self) -> None:
        if self.with_scheduler and self.scheduler is None:
            self.scheduler = DailyScheduler(self.ctx)
            self.scheduler.start()

    def engine(self) -> Engine:
        return Engine(self.ctx)


STATE: AppState | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "paper-radar/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> AppState:
        assert STATE is not None
        return STATE

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认日志，避免刷屏
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _authorized(self, query: dict) -> bool:
        token = str(self.state.ctx.cfg.get("app.token", "") or "")
        if not token:
            return True
        provided = self.headers.get("X-Token") or (query.get("token", [""])[0] if query else "")
        return provided == token

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if not self._authorized(query):
                    return self._error("unauthorized", 401)
                return self._api_get(path, query)
            return self._static(path)
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_HEAD(self) -> None:  # noqa: N802
        return self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if not self._authorized(query):
                return self._error("unauthorized", 401)
            return self._api_post(path, self._read_json())
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not self._authorized({}):
                return self._error("unauthorized", 401)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "topics"]:
                self.state.ctx.store.delete_topic(int(parts[2]))
                return self._json({"ok": True})
            return self._error("未知接口", 404)
        except Exception as exc:  # noqa: BLE001
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    # ------------------------------------------------------------------ #
    # 静态文件
    # ------------------------------------------------------------------ #
    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            target = WEB_DIR / "index.html"
        elif path.startswith("/reports/"):
            name = posixpath.basename(path)
            target = self.state.ctx.cfg.reports_dir() / name
        else:
            name = posixpath.basename(path)
            target = WEB_DIR / name
        if not target.exists() or not target.is_file():
            return self._error("未找到：" + path, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ------------------------------------------------------------------ #
    # GET API
    # ------------------------------------------------------------------ #
    def _api_get(self, path: str, query: dict) -> None:
        ctx = self.state.ctx
        if path == "/api/state":
            cfg = ctx.cfg
            return self._json(
                {
                    "ok": True,
                    "app": {
                        "name": cfg.get("app.name"),
                        "host": cfg.get("app.host"),
                        "port": cfg.get("app.port"),
                        "version": cfg.get("app.version", "1.0.0"),
                    },
                    "topics": [t.to_dict() for t in ctx.store.list_topics()],
                    "sources": available_sources(cfg),
                    "stats": ctx.store.stats(),
                    "runs": ctx.store.recent_runs(10),
                    "jobs": self.state.jobs.list(10),
                    "scheduler": self.state.scheduler.status() if self.state.scheduler else {"enabled": False},
                    "zotero": ctx.zotero.status(),
                    "mail": {
                        "enabled": ctx.mailer.enabled(),
                        "transport": ctx.mailer.transport.style,
                        "to": ctx.mailer.recipients(),
                    },
                    "llm": {
                        "provider": cfg.get("llm.provider"),
                        "fallback": cfg.get("llm.fallback_provider"),
                        "model": cfg.get("llm.model"),
                        "has_key": bool(cfg.get("llm.api_key")),
                    },
                    "search": {
                        "top_n": cfg.get("search.top_n"),
                        "year_lookback": cfg.get("search.year_lookback"),
                    },
                    "digest": cfg.get("digest", {}),
                    "config_masked": cfg.as_dict(redact=True),
                }
            )
        if path == "/api/topics":
            return self._json({"ok": True, "topics": [t.to_dict() for t in ctx.store.list_topics()]})
        if path.startswith("/api/topics/"):
            topic_id = int(path.rsplit("/", 1)[-1])
            topic = ctx.store.get_topic(topic_id)
            if not topic:
                return self._error("主题不存在", 404)
            return self._json(
                {
                    "ok": True,
                    "topic": topic.to_dict(),
                    "recommendations": ctx.store.list_recommendations(topic_id, limit=50),
                }
            )
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": self.state.jobs.list(20)})
        if path.startswith("/api/jobs/"):
            job = self.state.jobs.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._error("任务不存在", 404)
            return self._json({"ok": True, "job": job.to_dict()})
        if path == "/api/zotero/collections":
            try:
                collections = ctx.zotero.list_collections(refresh=True)
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc), "collections": []})
            return self._json({"ok": True, "collections": collections, "status": ctx.zotero.status()})
        if path == "/api/reports":
            reports = sorted(
                (p for p in ctx.cfg.reports_dir().glob("*.html")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return self._json(
                {
                    "ok": True,
                    "reports": [
                        {
                            "name": p.name,
                            "path": str(p),
                            "url": f"/reports/{p.name}",
                            "mtime": p.stat().st_mtime,
                        }
                        for p in reports[:30]
                    ],
                }
            )
        if path.startswith("/api/papers/"):
            key = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            paper = ctx.store.get_paper(key)
            if not paper:
                return self._error("论文不存在", 404)
            return self._json({"ok": True, "paper": paper.to_dict()})
        return self._error("未知接口 " + path, 404)

    # ------------------------------------------------------------------ #
    # POST API
    # ------------------------------------------------------------------ #
    def _api_post(self, path: str, body: dict) -> None:
        ctx = self.state.ctx
        engine = self.state.engine()

        if path == "/api/topics":
            topic = default_topic_from_payload(body)
            topic.id = ctx.store.save_topic(topic)
            return self._json({"ok": True, "topic": topic.to_dict()})

        if path.endswith("/build") and path.startswith("/api/topics/"):
            topic_id = int(path.split("/")[3])
            topic = ctx.store.get_topic(topic_id)
            if not topic:
                return self._error("主题不存在", 404)
            patch = body.get("topic")
            if patch:
                topic = default_topic_from_payload({**topic.to_dict(), **patch, "id": topic_id})

            def _run(job):
                job.log("开始生成方向画像…")
                result = engine.build_profile(topic, progress=job.log)
                return {"topic": result.to_dict()}

            job = self.state.jobs.submit("build_profile", _run, {"topic_id": topic_id})
            return self._json({"ok": True, "job": job.to_dict()})

        if path == "/api/topics/preview":
            topic = default_topic_from_payload(body)

            def _run_preview(job):
                job.log("解析方向并生成检索式…")
                topic2 = engine.build_profile(topic, progress=job.log)
                job.log("开始检索…")
                result = engine.search(
                    topic2,
                    limit=int(body.get("limit") or ctx.cfg.get("search.top_n", 15)),
                    sources=body.get("sources"),
                    summarize=bool(body.get("summarize", True)),
                    record=False,
                    progress=job.log,
                )
                return {"topic": topic2.to_dict(), **{k: v for k, v in result.items() if k != "topic"}}

            job = self.state.jobs.submit("preview", _run_preview)
            return self._json({"ok": True, "job": job.to_dict()})

        if path == "/api/search":
            topic_id = body.get("topic_id")
            topic = ctx.store.get_topic(int(topic_id)) if topic_id else default_topic_from_payload(body)
            if not topic:
                return self._error("主题不存在", 404)
            # 允许在检索时临时覆盖关键词/检索式（UI 上可以直接改）
            if body.get("queries"):
                topic.queries = [str(q) for q in body["queries"] if str(q).strip()]
            if body.get("keywords"):
                topic.keywords = [str(k) for k in body["keywords"] if str(k).strip()]
            if body.get("seeds"):
                topic = default_topic_from_payload({**topic.to_dict(), "seeds": body["seeds"]})

            def _run_search(job):
                if body.get("rebuild") or not topic.queries:
                    job.log("先补齐检索式…")
                    engine.build_profile(topic, progress=job.log)
                return engine.search(
                    topic,
                    limit=int(body.get("limit") or ctx.cfg.get("search.top_n", 15)),
                    sources=body.get("sources"),
                    summarize=bool(body.get("summarize", True)),
                    record=body.get("record", True),
                    progress=job.log,
                )

            job = self.state.jobs.submit("search", _run_search, {"topic_id": topic.id})
            return self._json({"ok": True, "job": job.to_dict()})

        if path == "/api/zotero/add":
            keys = [str(k) for k in (body.get("keys") or [])]
            result = engine.add_to_zotero(
                keys,
                collection=body.get("collection", ""),
                tags=body.get("tags"),
                topic_id=body.get("topic_id"),
            )
            return self._json({"ok": True, "result": result})

        if path == "/api/digest/run":
            send = bool(body.get("send", False))
            topic_ids = body.get("topic_ids") or None

            def _run_digest(job):
                return run_digest(
                    ctx,
                    topic_ids=[int(x) for x in topic_ids] if topic_ids else None,
                    send=send,
                    progress=job.log,
                )

            job = self.state.jobs.submit("digest", _run_digest, {"send": send})
            return self._json({"ok": True, "job": job.to_dict()})

        if path == "/api/settings":
            patch = body.get("patch") or {}
            secrets = body.get("secrets") or {}
            if patch:
                ctx.cfg.save(patch)
            if secrets:
                save_secrets(secrets)
            ctx.reload()
            return self._json({"ok": True, "config": ctx.cfg.as_dict(redact=True)})

        if path == "/api/sources/test":
            from .net import Fetcher
            from .sources import build_sources

            fetcher = Fetcher(ctx.cfg)

            def _run_test(job):
                out = {}
                for plugin in build_sources(ctx.cfg, fetcher):
                    job.log(f"测试 {plugin.label} …")
                    try:
                        found = plugin.search(
                            body.get("query") or "buffer management switch",
                            limit=3,
                        )
                        out[plugin.name] = {"ok": True, "count": len(found), "sample": [p.title for p in found[:2]]}
                    except Exception as exc:  # noqa: BLE001
                        out[plugin.name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                return out

            job = self.state.jobs.submit("sources_test", _run_test)
            return self._json({"ok": True, "job": job.to_dict()})

        return self._error("未知接口 " + path, 404)


def create_server(ctx: Context | None = None, *, with_scheduler: bool = True) -> ThreadingHTTPServer:
    global STATE
    STATE = AppState(ctx, with_scheduler=with_scheduler)
    STATE.start()  # 内置定时器：到点自动跑每日简报
    cfg = STATE.ctx.cfg
    host = cfg.get("app.host", "127.0.0.1")
    port = int(cfg.get("app.port", 8848))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve_forever(ctx: Context | None = None, *, with_scheduler: bool = True) -> None:
    httpd = create_server(ctx, with_scheduler=with_scheduler)
    host, port = httpd.server_address[:2]
    print(f"Paper Radar 控制台已启动： http://{host}:{port}")
    if STATE and STATE.scheduler:
        print(f"每日简报定时： {STATE.scheduler.status()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在关闭…")
    finally:
        if STATE and STATE.scheduler:
            STATE.scheduler.stop()
        httpd.server_close()


__all__ = ["create_server", "serve_forever", "AppState", "get_config"]
