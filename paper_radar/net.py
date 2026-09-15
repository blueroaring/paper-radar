"""HTTP 工具：标准库 urllib 封装，带重试、退避、按域名限速、可选代理。

论文站点（尤其是 Google Scholar）对频率和 UA 敏感，所以限速与退避逻辑集中在这里，
所有数据源插件共用，避免每个插件各写一遍。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class FetchError(RuntimeError):
    """网络层失败（已重试完毕）。数据源插件应捕获它并降级，而不是让整次检索崩掉。"""

    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class Fetcher:
    def __init__(self, cfg):
        http = cfg.get("sources.http", {}) or {}
        self.user_agent = http.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        self.timeout = float(http.get("timeout", 25))
        self.host_timeout: dict[str, float] = http.get("host_timeout", {}) or {}
        self.retries = int(http.get("retries", 3))
        self.host_retries: dict[str, int] = http.get("host_retries", {}) or {}
        self.backoff = float(http.get("backoff", 1.8))
        self.min_interval: dict[str, float] = http.get("min_interval", {}) or {}
        self.proxy = (http.get("proxy") or "").strip()
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

        handlers: list[Any] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------ #
    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
            return self._locks[host]

    def _throttle(self, host: str) -> None:
        interval = float(self.min_interval.get(host, self.min_interval.get("default", 0.0)) or 0.0)
        if interval <= 0:
            return
        lock = self._lock_for(host)
        with lock:
            last = self._last_hit.get(host, 0.0)
            wait = interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.time()

    # ------------------------------------------------------------------ #
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        accept: str = "*/*",
    ) -> bytes:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        host = urllib.parse.urlparse(url).netloc
        hdrs = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)

        last_error: Exception | None = None
        timeout = float(self.host_timeout.get(host, self.timeout))
        attempts = int(self.host_retries.get(host, self.retries))
        for attempt in range(max(1, attempts)):
            self._throttle(host)
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(self.backoff ** (attempt + 1))
                    continue
                raise FetchError(f"HTTP {exc.code} {exc.reason}", status=exc.code, url=url) from exc
            except Exception as exc:  # 超时 / DNS / 连接重置
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(self.backoff ** (attempt + 1))
                    continue
        raise FetchError(f"请求失败: {last_error}", url=url)

    # ------------------------------------------------------------------ #
    def get_text(self, url: str, **kwargs: Any) -> str:
        raw = self.get(url, **kwargs)
        charset = "utf-8"
        head = raw[:2048].decode("latin-1", errors="ignore").lower()
        if "charset=" in head:
            charset = head.split("charset=")[1].split(";")[0].split('"')[0].strip() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("accept", "application/json")
        return json.loads(self.get_text(url, **kwargs))

    # 便于测试替换 ------------------------------------------------------ #
    def post_json(
        self,
        url: str,
        payload: Any,
        headers: dict[str, str] | None = None,
        *,
        method: str = "POST",
    ) -> tuple[int, Any]:
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"User-Agent": self.user_agent, "Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, {"error": body}
