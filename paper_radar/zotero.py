"""Zotero Web API 客户端：把推荐表里勾选的论文写进你的文献库。

为什么走 Web API 而不是本地接口：
  * 官方、稳定，Zotero 桌面端没开也能用（下次同步自动回本地库）；
  * 与既有 `~/.dsh/zotero-key` 的习惯兼容（config 里可配 api_key_file）；
  * 语义清晰：DOI 去重 → 建条目 → 归入分类 → 打标签。

注意：本模块**只读配置里的密钥**，不打印、不写日志。
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any

from .models import Paper
from .net import Fetcher

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def split_name(name: str) -> dict[str, str]:
    """尽量把 "Zhiyu Zhang" 拆成 firstName/lastName；中文名整体作为 lastName。"""
    name = (name or "").strip()
    if not name:
        return {"creatorType": "author", "firstName": "", "lastName": ""}
    if _CJK_RE.search(name):
        return {"creatorType": "author", "firstName": "", "lastName": name}
    if "," in name:  # "Zhang, Zhiyu"
        last, _, first = name.partition(",")
        return {"creatorType": "author", "firstName": first.strip(), "lastName": last.strip()}
    parts = name.split()
    if len(parts) == 1:
        return {"creatorType": "author", "firstName": "", "lastName": parts[0]}
    return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def paper_to_zotero_item(
    paper: Paper,
    *,
    collections: list[str] | None = None,
    tags: list[str] | None = None,
    item_type_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """纯函数：Paper → Zotero item JSON。方便单元测试，也方便你按需改字段映射。"""
    item_type_map = item_type_map or {}
    item_type = item_type_map.get(paper.item_type, paper.item_type) or "journalArticle"
    date = str(paper.year) if paper.year else ""
    creators = [split_name(a) for a in paper.authors if a and a.strip()]

    item: dict[str, Any] = {
        "itemType": item_type,
        "title": paper.title,
        "creators": creators,
        "abstractNote": paper.abstract or "",
        "date": date,
        "DOI": paper.doi or "",
        "url": paper.url or "",
        "extra": _build_extra(paper),
        "tags": [{"tag": t} for t in (tags or []) if t],
        "collections": list(collections or []),
    }
    if item_type == "conferencePaper":
        item["proceedingsTitle"] = paper.venue or ""
        item["conferenceName"] = paper.venue or ""
    elif item_type == "preprint":
        item["repository"] = "arXiv" if paper.arxiv_id else (paper.venue or "")
        item["archiveID"] = f"arXiv:{paper.arxiv_id}" if paper.arxiv_id else ""
        item["libraryCatalog"] = paper.venue or ""
    else:
        item["publicationTitle"] = paper.venue or ""
    if paper.venue_detail:
        item["extra"] = (item["extra"] + "\n" + paper.venue_detail).strip()
    return {k: v for k, v in item.items() if v not in ("", None, [], {}) or k in ("title", "itemType")}


def _build_extra(paper: Paper) -> str:
    parts = []
    if paper.sources:
        parts.append("发现于: " + ", ".join(paper.sources))
    if paper.citations is not None:
        parts.append(f"被引: {paper.citations}")
    if paper.extra.get("venue_tier") and paper.extra["venue_tier"] != "unknown":
        parts.append(f"分区: {paper.extra['venue_tier']}")
    return "\n".join(parts)


class ZoteroClient:
    def __init__(self, cfg, fetcher: Fetcher | None = None):
        self.cfg = cfg
        self.http = fetcher or Fetcher(cfg)
        self.base = (cfg.get("zotero.api_base", "https://api.zotero.org") or "").rstrip("/")
        self.api_key = (cfg.get("zotero.api_key", "") or "").strip()
        self._user_id = str(cfg.get("zotero.user_id", "") or "")
        self._collections_cache: list[dict] | None = None

    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        return bool(self.cfg.get("zotero.enabled", True)) and bool(self.api_key)

    def status(self) -> dict:
        return {
            "enabled": bool(self.cfg.get("zotero.enabled", True)),
            "has_key": bool(self.api_key),
            "user_id": self._user_id,
            "api_base": self.base,
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Zotero-API-Version": "3"}

    def user_id(self) -> str:
        """user_id 未配置时，用 key 自身换取（GET /keys/<key>），避免让用户手填数字 ID。"""
        if self._user_id and self._user_id != "0":
            return self._user_id
        if not self.api_key:
            raise RuntimeError("未配置 Zotero API key（config.zotero.api_key 或 api_key_file）")
        data = self.http.get_json(
            f"{self.base}/keys/{self.api_key}", headers={"Zotero-API-Version": "3"}
        )
        self._user_id = str((data or {}).get("userID", ""))
        if not self._user_id:
            raise RuntimeError("无法从 key 解析 userID，请在 config 里显式填写 zotero.user_id")
        return self._user_id

    def _url(self, path: str) -> str:
        return f"{self.base}/users/{self.user_id()}{path}"

    # ------------------------------------------------------------------ #
    def list_collections(self, refresh: bool = False) -> list[dict]:
        if self._collections_cache is not None and not refresh:
            return self._collections_cache
        if not self.enabled():
            return []
        data = self.http.get_json(
            self._url("/collections"),
            params={"limit": 100, "format": "json"},
            headers=self._headers(),
        )
        items = []
        for entry in data or []:
            d = entry.get("data", {}) if isinstance(entry, dict) else {}
            items.append(
                {
                    "key": entry.get("key", ""),
                    "name": d.get("name", ""),
                    "parent": d.get("parentCollection") or "",
                    "count": (entry.get("meta") or {}).get("numItems", 0),
                }
            )
        self._collections_cache = items
        return items

    def resolve_collection(self, name_or_key: str) -> str:
        """既接受分类 key，也接受分类名（找不到时返回空串，不抛异常）。"""
        if not name_or_key:
            return ""
        if not self.enabled():
            return ""
        for coll in self.list_collections():
            if coll["key"] == name_or_key or coll["name"] == name_or_key:
                return coll["key"]
        return ""

    def find_duplicate(self, paper: Paper) -> str | None:
        """在库里找同一篇论文（先按 DOI，再按标题模糊匹配），避免重复入库。"""
        if not self.enabled():
            return None
        query = paper.doi or paper.title
        if not query:
            return None
        try:
            data = self.http.get_json(
                self._url("/items"),
                params={"q": query[:120], "qmode": "everything", "limit": 10, "format": "json"},
                headers=self._headers(),
            )
        except Exception:  # noqa: BLE001 - 查重失败不应阻止入库
            return None
        doi = (paper.doi or "").lower()
        title_norm = re.sub(r"[^a-z0-9]+", "", (paper.title or "").lower())[:60]
        for entry in data or []:
            d = entry.get("data", {}) if isinstance(entry, dict) else {}
            if doi and (d.get("DOI") or "").lower() == doi:
                return entry.get("key")
            other = re.sub(r"[^a-z0-9]+", "", (d.get("title") or "").lower())[:60]
            if title_norm and other and (title_norm == other or title_norm in other or other in title_norm):
                return entry.get("key")
        return None

    def add_papers(
        self,
        papers: list[Paper],
        *,
        collection: str = "",
        tags: list[str] | None = None,
        skip_duplicates: bool = True,
    ) -> dict:
        """批量入库。返回 {added, duplicates, failed, items}，UI 直接拿来显示。"""
        if not self.enabled():
            return {"added": 0, "duplicates": 0, "failed": len(papers), "error": "Zotero 未配置或未启用", "items": []}

        collection_key = self.resolve_collection(collection) or self.resolve_collection(
            self.cfg.get("zotero.default_collection", "")
        )
        tag_list = tags if tags is not None else list(self.cfg.get("zotero.default_tags", []) or [])
        collections = [collection_key] if collection_key else []

        result = {"added": 0, "duplicates": 0, "failed": 0, "items": [], "error": ""}
        to_create: list[tuple[Paper, dict]] = []
        for paper in papers:
            if skip_duplicates and self.find_duplicate(paper):
                result["duplicates"] += 1
                result["items"].append({"key": paper.key, "title": paper.title, "status": "duplicate"})
                continue
            to_create.append(
                (
                    paper,
                    paper_to_zotero_item(
                        paper,
                        collections=collections,
                        tags=tag_list,
                        item_type_map=self.cfg.get("zotero.item_type_map", {}) or {},
                    ),
                )
            )

        # Zotero 单次最多 50 条，分批提交
        for start in range(0, len(to_create), 50):
            chunk = to_create[start : start + 50]
            status, data = self.http.post_json(
                self._url("/items"),
                [item for _paper, item in chunk],
                headers=self._headers(),
            )
            if status >= 400:
                result["failed"] += len(chunk)
                result["error"] = str(data)[:300]
                for paper, _item in chunk:
                    result["items"].append({"key": paper.key, "title": paper.title, "status": "failed"})
                continue
            successful = (data or {}).get("successful") or {}
            failed = (data or {}).get("failed") or {}
            result["failed"] += len(failed)
            for idx, (paper, _item) in enumerate(chunk):
                if str(idx) in successful:
                    entry = successful[str(idx)]
                    result["added"] += 1
                    result["items"].append(
                        {
                            "key": paper.key,
                            "title": paper.title,
                            "status": "added",
                            "zotero_key": entry.get("key", ""),
                            "zotero_version": entry.get("version"),
                        }
                    )
                elif str(idx) in failed:
                    result["items"].append({"key": paper.key, "title": paper.title, "status": "failed"})
        return result

    def delete_items(self, entries: list[dict]) -> dict:
        """删除刚刚写入的条目（entries 来自 add_papers 的 items，含 zotero_key / zotero_version）。

        用途：自检脚本写入后再回滚，验证写权限但不给你的文库留垃圾。
        """
        if not self.enabled():
            return {"ok": False, "error": "Zotero 未配置"}
        deleted, failed = 0, []
        for entry in entries:
            zkey = entry.get("zotero_key")
            if not zkey:
                continue
            headers = self._headers()
            version = entry.get("zotero_version")
            if version is not None:
                headers["If-Unmodified-Since-Version"] = str(version)
            req = urllib.request.Request(
                self._url(f"/items/{zkey}"), headers=headers, method="DELETE"
            )
            try:
                with self.http._opener.open(req, timeout=self.http.timeout) as resp:  # noqa: SLF001
                    if resp.status < 400:
                        deleted += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{zkey}: {exc}")
        return {"ok": not failed, "deleted": deleted, "failed": failed}

    def create_collection(self, name: str, parent: str = "") -> str:
        if not self.enabled():
            raise RuntimeError("Zotero 未配置")
        payload = [{"name": name, "parentCollection": parent or False}]
        status, data = self.http.post_json(self._url("/collections"), payload, headers=self._headers())
        if status >= 400:
            raise RuntimeError(f"创建分类失败: {str(data)[:200]}")
        self._collections_cache = None
        successful = (data or {}).get("successful") or {}
        for _idx, value in successful.items():
            return value.get("key", "")
        return ""
