"""诊断各外部站点的可达性（用与 paper-radar 相同的 urllib 栈）。"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TARGETS = [
    ("arxiv-api-https", "https://export.arxiv.org/api/query?search_query=all:buffer&max_results=1"),
    ("arxiv-api-http", "http://export.arxiv.org/api/query?search_query=all:buffer&max_results=1"),
    ("arxiv-alt-host", "https://arxiv.org/api/query?search_query=all:buffer&max_results=1"),
    ("arxiv-rss", "https://rss.arxiv.org/rss/cs.NI"),
    ("dblp-https", "https://dblp.org/search/publ/api?q=buffer+management&format=json&h=2"),
    ("dblp-http", "http://dblp.org/search/publ/api?q=buffer+management&format=json&h=2"),
    ("dblp-xml", "https://dblp.org/search/publ/api?q=buffer+management&h=2"),
    ("dblp-triertls", "https://dblp.uni-trier.de/search/publ/api?q=buffer+management&format=json&h=2"),
    ("s2", "https://api.semanticscholar.org/graph/v1/paper/search?query=buffer&limit=1&fields=title"),
    ("openalex", "https://api.openalex.org/works?search=buffer&per-page=1"),
    ("scholar", "https://scholar.google.com/scholar?q=buffer+management&hl=en"),
]

for name, url in TARGETS:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(400)
            print(f"[OK ] {name:<16} {resp.status} len={len(body)} head={body[:70]!r}")
    except urllib.error.HTTPError as exc:
        print(f"[HTTP] {name:<16} {exc.code} {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name:<16} {type(exc).__name__}: {exc}")

print("\n-- 本地代理端口探测（v2rayN 常见端口）--")
for port in (10808, 10809, 10810, 7890, 7891, 8889, 20171):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        print(f"  127.0.0.1:{port} 开放")
    except Exception:  # noqa: BLE001
        print(f"  127.0.0.1:{port} 关闭")
    finally:
        s.close()
