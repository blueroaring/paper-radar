"""SQLite 存储层：主题、论文目录、推荐记录、每日发送记录、运行日志。

只用标准库 sqlite3。所有 JSON 字段统一用 _dumps/_loads 处理，避免各表各写一遍。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Paper, Recommendation, Seed, Topic

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    direction TEXT DEFAULT '',
    seeds TEXT DEFAULT '[]',
    keywords TEXT DEFAULT '[]',
    negative_keywords TEXT DEFAULT '[]',
    queries TEXT DEFAULT '[]',
    filters TEXT DEFAULT '{}',
    zotero_collection TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    profile_summary TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT DEFAULT '[]',
    year INTEGER,
    venue TEXT DEFAULT '',
    venue_detail TEXT DEFAULT '',
    doi TEXT DEFAULT '',
    arxiv_id TEXT DEFAULT '',
    url TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    item_type TEXT DEFAULT 'journalArticle',
    citations INTEGER,
    source TEXT DEFAULT '',
    sources TEXT DEFAULT '[]',
    extra TEXT DEFAULT '{}',
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS recommendations (
    topic_id INTEGER NOT NULL,
    paper_key TEXT NOT NULL,
    score REAL DEFAULT 0,
    score_parts TEXT DEFAULT '{}',
    summary TEXT DEFAULT '',
    highlights TEXT DEFAULT '[]',
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'shown',
    provider TEXT DEFAULT '',
    created_at TEXT,
    PRIMARY KEY (topic_id, paper_key)
);

CREATE INDEX IF NOT EXISTS idx_rec_topic_created ON recommendations(topic_id, created_at DESC);

-- 本地记录"这篇论文已经进了 Zotero"。
-- 为什么需要它：Zotero Web API 的搜索索引有延迟，刚写入的条目立刻按 DOI/标题查重会查不到，
-- 用户连点两次「加入 Zotero」就会产生重复条目。本地记住映射即可立刻幂等；
-- 远端查重仍然保留，用来兜住"在别处手动加过"的情况。
CREATE TABLE IF NOT EXISTS zotero_links (
    paper_key TEXT PRIMARY KEY,
    zotero_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    topic_id INTEGER,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    stats TEXT DEFAULT '{}',
    report_path TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: str, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return fallback


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def save_topic(self, topic: Topic) -> int:
        payload = (
            topic.name.strip() or "未命名方向",
            topic.direction or "",
            _dumps([s.__dict__ for s in topic.seeds]),
            _dumps(topic.keywords),
            _dumps(topic.negative_keywords),
            _dumps(topic.queries),
            _dumps(topic.filters),
            topic.zotero_collection or "",
            1 if topic.enabled else 0,
            topic.profile_summary or "",
        )
        with self._lock:
            if topic.id:
                self.conn.execute(
                    """UPDATE topics SET name=?, direction=?, seeds=?, keywords=?, negative_keywords=?,
                       queries=?, filters=?, zotero_collection=?, enabled=?, profile_summary=?, updated_at=?
                       WHERE id=?""",
                    (*payload, now_iso(), topic.id),
                )
                topic_id = topic.id
            else:
                cur = self.conn.execute(
                    """INSERT INTO topics (name, direction, seeds, keywords, negative_keywords, queries,
                       filters, zotero_collection, enabled, profile_summary, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*payload, now_iso(), now_iso()),
                )
                topic_id = int(cur.lastrowid)
            self.conn.commit()
        return topic_id

    def list_topics(self, only_enabled: bool = False) -> list[Topic]:
        sql = "SELECT * FROM topics"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [_row_to_topic(row) for row in rows]

    def get_topic(self, topic_id: int) -> Topic | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return _row_to_topic(row) if row else None

    def find_topic_by_name(self, name: str) -> Topic | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM topics WHERE name = ? ORDER BY id LIMIT 1", (name,)
            ).fetchone()
        return _row_to_topic(row) if row else None

    def delete_topic(self, topic_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM recommendations WHERE topic_id = ?", (topic_id,))
            self.conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # 论文
    # ------------------------------------------------------------------ #
    def upsert_papers(self, papers: Iterable[Paper]) -> int:
        count = 0
        stamp = now_iso()
        with self._lock:
            for paper in papers:
                if not paper.title:
                    continue
                self.conn.execute(
                    """INSERT INTO papers (key, title, authors, year, venue, venue_detail, doi, arxiv_id,
                       url, abstract, item_type, citations, source, sources, extra, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         title=excluded.title,
                         authors=excluded.authors,
                         year=COALESCE(excluded.year, papers.year),
                         venue=CASE WHEN excluded.venue != '' THEN excluded.venue ELSE papers.venue END,
                         doi=CASE WHEN excluded.doi != '' THEN excluded.doi ELSE papers.doi END,
                         arxiv_id=CASE WHEN excluded.arxiv_id != '' THEN excluded.arxiv_id ELSE papers.arxiv_id END,
                         url=CASE WHEN excluded.url != '' THEN excluded.url ELSE papers.url END,
                         abstract=CASE WHEN length(excluded.abstract) > length(papers.abstract)
                                       THEN excluded.abstract ELSE papers.abstract END,
                         item_type=excluded.item_type,
                         citations=MAX(COALESCE(excluded.citations,0), COALESCE(papers.citations,0)),
                         sources=excluded.sources,
                         extra=excluded.extra,
                         last_seen=excluded.last_seen""",
                    (
                        paper.key,
                        paper.title,
                        _dumps(paper.authors),
                        paper.year,
                        paper.venue,
                        paper.venue_detail,
                        paper.doi,
                        paper.arxiv_id,
                        paper.url,
                        paper.abstract,
                        paper.item_type,
                        paper.citations,
                        paper.source,
                        _dumps(paper.sources or [paper.source]),
                        _dumps(paper.extra),
                        stamp,
                        stamp,
                    ),
                )
                count += 1
            self.conn.commit()
        return count

    def get_paper(self, key: str) -> Paper | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM papers WHERE key = ?", (key,)).fetchone()
        return _row_to_paper(row) if row else None

    def count_papers(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"])

    # ------------------------------------------------------------------ #
    # 推荐记录
    # ------------------------------------------------------------------ #
    def save_recommendations(
        self, topic_id: int, recs: list[Recommendation], *, status: str = "shown", provider: str = ""
    ) -> None:
        stamp = now_iso()
        with self._lock:
            for rec in recs:
                self.conn.execute(
                    """INSERT INTO recommendations (topic_id, paper_key, score, score_parts, summary,
                       highlights, reason, status, provider, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(topic_id, paper_key) DO UPDATE SET
                         score=excluded.score, score_parts=excluded.score_parts,
                         summary=CASE WHEN excluded.summary != '' THEN excluded.summary ELSE recommendations.summary END,
                         highlights=excluded.highlights,
                         reason=CASE WHEN excluded.reason != '' THEN excluded.reason ELSE recommendations.reason END,
                         status=CASE WHEN recommendations.status = 'accepted' THEN 'accepted' ELSE excluded.status END,
                         provider=excluded.provider""",
                    (
                        topic_id,
                        rec.paper.key,
                        rec.score,
                        _dumps(rec.score_parts),
                        rec.summary,
                        _dumps(rec.highlights),
                        rec.reason,
                        status,
                        provider,
                        stamp,
                    ),
                )
            self.conn.commit()

    def is_new_for_topic(self, topic_id: int, paper_key: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM recommendations WHERE topic_id = ? AND paper_key = ? LIMIT 1",
                (topic_id, paper_key),
            ).fetchone()
        return row is None

    def filter_new(self, topic_id: int, papers: list[Paper]) -> list[Paper]:
        return [p for p in papers if self.is_new_for_topic(topic_id, p.key)]

    def list_recommendations(self, topic_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.*, p.title, p.authors, p.year, p.venue, p.doi, p.arxiv_id, p.url,
                          p.abstract, p.citations, p.item_type, p.sources, p.extra
                   FROM recommendations r JOIN papers p ON p.key = r.paper_key
                   WHERE r.topic_id = ? ORDER BY r.score DESC, r.created_at DESC LIMIT ?""",
                (topic_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "key": row["paper_key"],
                    "title": row["title"],
                    "authors": _loads(row["authors"], []),
                    "year": row["year"],
                    "venue": row["venue"],
                    "doi": row["doi"],
                    "arxiv_id": row["arxiv_id"],
                    "url": row["url"],
                    "abstract": row["abstract"],
                    "citations": row["citations"],
                    "item_type": row["item_type"],
                    "sources": _loads(row["sources"], []),
                    "extra": _loads(row["extra"], {}),
                    "score": row["score"],
                    "score_parts": _loads(row["score_parts"], {}),
                    "summary": row["summary"],
                    "highlights": _loads(row["highlights"], []),
                    "reason": row["reason"],
                    "status": row["status"],
                    "provider": row["provider"],
                    "created_at": row["created_at"],
                }
            )
        return out

    def set_status(self, topic_id: int, paper_key: str, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE recommendations SET status = ? WHERE topic_id = ? AND paper_key = ?",
                (status, topic_id, paper_key),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # Zotero 入库记录
    # ------------------------------------------------------------------ #
    def link_zotero(self, paper_key: str, zotero_key: str = "", title: str = "") -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO zotero_links (paper_key, zotero_key, title, added_at) VALUES (?,?,?,?)
                   ON CONFLICT(paper_key) DO UPDATE SET
                     zotero_key=excluded.zotero_key, title=excluded.title, added_at=excluded.added_at""",
                (paper_key, zotero_key, title, now_iso()),
            )
            self.conn.commit()

    def zotero_links(self) -> dict[str, dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM zotero_links").fetchall()
        return {
            row["paper_key"]: {
                "zotero_key": row["zotero_key"],
                "title": row["title"],
                "added_at": row["added_at"],
            }
            for row in rows
        }

    def is_linked_zotero(self, paper_key: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM zotero_links WHERE paper_key = ? LIMIT 1", (paper_key,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # 运行日志
    # ------------------------------------------------------------------ #
    def start_run(self, kind: str, topic_id: int | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs (kind, topic_id, started_at, status) VALUES (?,?,?,?)",
                (kind, topic_id, now_iso(), "running"),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "ok",
        stats: dict | None = None,
        report_path: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE runs SET finished_at=?, status=?, stats=?, report_path=?, error=? WHERE id=?""",
                (now_iso(), status, _dumps(stats or {}), report_path, error[:2000], run_id),
            )
            self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "topic_id": row["topic_id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "stats": _loads(row["stats"], {}),
                "report_path": row["report_path"],
                "error": row["error"],
            }
            for row in rows
        ]

    def stats(self) -> dict:
        with self._lock:
            papers = self.conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"]
            topics = self.conn.execute("SELECT COUNT(*) AS c FROM topics").fetchone()["c"]
            recs = self.conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"]
            accepted = self.conn.execute(
                "SELECT COUNT(*) AS c FROM recommendations WHERE status='accepted'"
            ).fetchone()["c"]
        return {"papers": papers, "topics": topics, "recommendations": recs, "accepted": accepted}


# --------------------------------------------------------------------------- #
# 行 → 模型
# --------------------------------------------------------------------------- #


def _row_to_topic(row: sqlite3.Row) -> Topic:
    seeds = []
    for raw in _loads(row["seeds"], []):
        if isinstance(raw, dict):
            seeds.append(Seed(**{k: raw.get(k, "") for k in ("value", "title", "doi", "arxiv_id", "url")}))
        elif isinstance(raw, str):
            seeds.append(Seed(value=raw))
    return Topic(
        id=row["id"],
        name=row["name"],
        direction=row["direction"] or "",
        seeds=seeds,
        keywords=_loads(row["keywords"], []),
        negative_keywords=_loads(row["negative_keywords"], []),
        queries=_loads(row["queries"], []),
        filters=_loads(row["filters"], {}),
        zotero_collection=row["zotero_collection"] or "",
        enabled=bool(row["enabled"]),
        profile_summary=row["profile_summary"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def _row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(
        title=row["title"],
        authors=_loads(row["authors"], []),
        year=row["year"],
        venue=row["venue"] or "",
        venue_detail=row["venue_detail"] or "",
        doi=row["doi"] or "",
        arxiv_id=row["arxiv_id"] or "",
        url=row["url"] or "",
        abstract=row["abstract"] or "",
        item_type=row["item_type"] or "journalArticle",
        citations=row["citations"],
        source=row["source"] or "",
        sources=_loads(row["sources"], []),
        extra=_loads(row["extra"], {}),
    )
