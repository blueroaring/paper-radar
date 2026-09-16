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

from .models import Paper, Recommendation, Seed, Topic, normalize_title, venue_quality

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
    last_seen TEXT,
    title_norm TEXT DEFAULT ''
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

-- “高分记忆”：相关度达到阈值的论文自动记下来，跨检索、跨简报长期保留。
-- 与 recommendations 的区别：
--   recommendations 是“某个方向看过哪些论文”（用于去重、防止重复推送）；
--   remembered     是“用户真正想看第二遍的论文”（用于回看、导出、入库）。
CREATE TABLE IF NOT EXISTS remembered (
    paper_key TEXT PRIMARY KEY,
    topic_id INTEGER,
    topic_name TEXT DEFAULT '',
    score REAL DEFAULT 0,
    score_parts TEXT DEFAULT '{}',
    summary TEXT DEFAULT '',
    highlights TEXT DEFAULT '[]',
    reason TEXT DEFAULT '',
    note TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    origin TEXT DEFAULT 'search',
    first_seen TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_remembered_score ON remembered(score DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _key_rank(key: str) -> int:
    """主键"正式程度"排序：DOI(0) > arXiv(1) > 标题哈希(2)。

    合并重复论文时保留排名最小的那个 key —— DOI 是学术记录里最稳定的标识。
    """
    if key.startswith("doi:"):
        return 0
    if key.startswith("arxiv:"):
        return 1
    return 2


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
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL + busy_timeout：控制台和计划任务可能同时开库，
            # 默认的 journal 模式在这种并发下容易抛 "database is locked"。
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=15000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：老库补上后加的列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(papers)")}
        if "title_norm" not in columns:
            self.conn.execute("ALTER TABLE papers ADD COLUMN title_norm TEXT DEFAULT ''")
        # 回填历史行的归一化标题，否则跨运行去重对老数据不生效
        missing = self.conn.execute(
            "SELECT key, title FROM papers WHERE title_norm IS NULL OR title_norm = ''"
        ).fetchall()
        for row in missing:
            self.conn.execute(
                "UPDATE papers SET title_norm = ? WHERE key = ?",
                (normalize_title(row["title"]), row["key"]),
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_title_norm ON papers(title_norm)"
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ #
    # 论文身份（跨运行去重）
    # ------------------------------------------------------------------ #
    def title_key_index(self) -> dict[str, str]:
        """返回 {归一化标题: 已存在的 paper key}，用于把同文不同源的记录归到同一个 key。

        同一标题有多行时（老数据的历史遗留），优先取"更像正式记录"的那个 key：
        DOI > arXiv > 标题哈希。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT key, title_norm FROM papers WHERE title_norm IS NOT NULL AND title_norm != ''"
            ).fetchall()
        index: dict[str, str] = {}
        for row in rows:
            current = index.get(row["title_norm"])
            if current is None or _key_rank(row["key"]) < _key_rank(current):
                index[row["title_norm"]] = row["key"]
        return index

    def dedupe_papers(self) -> dict:
        """把历史遗留的重复论文行合并成一条，并把引用它的记录一并搬迁。

        触发场景：先由无 DOI 的数据源存了 `title:` 版本，后来有 DOI 的数据源又存了 `doi:` 版本。
        保留"更像正式记录"的 key（DOI > arXiv > 标题哈希），并把摘要最完整的那条内容并过去。
        幂等，可重复运行。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT key, title, title_norm, abstract, venue, year, citations FROM papers ORDER BY key"
            ).fetchall()
            groups: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                norm = row["title_norm"] or normalize_title(row["title"])
                groups.setdefault(norm, []).append(row)

            merged_groups = 0
            removed = 0
            for norm, members in groups.items():
                if len(members) < 2:
                    continue
                keep = min(members, key=lambda r: _key_rank(r["key"]))
                losers = [m for m in members if m["key"] != keep["key"]]

                # 逐字段取"最好的那个"：摘要最长、发表处最正式、引用数最高
                richest = max(members, key=lambda r: len(r["abstract"] or ""))
                best_venue = max(members, key=lambda r: venue_quality(r["venue"] or ""))
                citations = max((m["citations"] or 0) for m in members)
                years = [m["year"] for m in members if m["year"]]
                self.conn.execute(
                    """UPDATE papers SET abstract = ?, venue = ?, citations = ?, year = ?, title_norm = ?
                       WHERE key = ?""",
                    (
                        richest["abstract"] if len(richest["abstract"] or "") > len(keep["abstract"] or "")
                        else keep["abstract"],
                        best_venue["venue"] if venue_quality(best_venue["venue"] or "") > venue_quality(keep["venue"] or "")
                        else keep["venue"],
                        citations or None,
                        min(years) if years else keep["year"],
                        norm,
                        keep["key"],
                    ),
                )

                for loser in losers:
                    # 推荐记录是复合主键，用 INSERT OR IGNORE 搬迁，冲突就保留原有的那条
                    self.conn.execute(
                        """INSERT OR IGNORE INTO recommendations
                           (topic_id, paper_key, score, score_parts, summary, highlights,
                            reason, status, provider, created_at)
                           SELECT topic_id, ?, score, score_parts, summary, highlights,
                                  reason, status, provider, created_at
                           FROM recommendations WHERE paper_key = ?""",
                        (keep["key"], loser["key"]),
                    )
                    # 记忆与 Zotero 链接都是 paper_key 主键，同样 OR IGNORE 防止覆盖更好的记录
                    self.conn.execute(
                        """INSERT OR IGNORE INTO remembered
                           (paper_key, topic_id, topic_name, score, score_parts, summary,
                            highlights, reason, note, tags, origin, first_seen, updated_at)
                           SELECT ?, topic_id, topic_name, score, score_parts, summary,
                                  highlights, reason, note, tags, origin, first_seen, updated_at
                           FROM remembered WHERE paper_key = ?""",
                        (keep["key"], loser["key"]),
                    )
                    self.conn.execute(
                        """INSERT OR IGNORE INTO zotero_links (paper_key, zotero_key, title, added_at)
                           SELECT ?, zotero_key, title, added_at FROM zotero_links WHERE paper_key = ?""",
                        (keep["key"], loser["key"]),
                    )
                    for table in ("recommendations", "remembered", "zotero_links"):
                        self.conn.execute(f"DELETE FROM {table} WHERE paper_key = ?", (loser["key"],))
                    self.conn.execute("DELETE FROM papers WHERE key = ?", (loser["key"],))
                    removed += 1
                merged_groups += 1
            self.conn.commit()
            orphans = self.conn.execute(
                """SELECT COUNT(*) AS c FROM recommendations r
                   LEFT JOIN papers p ON p.key = r.paper_key WHERE p.key IS NULL"""
            ).fetchone()["c"]
        return {
            "groups_merged": merged_groups,
            "rows_removed": removed,
            "papers_left": self.count_papers(),
            "remembered": self.remembered_count(),
            # 历史遗留（早期版本每日简报没写 papers 表）。**故意不删**：
            # 它们仍承担"这篇已经推过，别再推"的记忆作用，删掉反而会重复推送。
            "orphan_recommendations": orphans,
        }

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
                # 发表处只升不降：已有的正式会议/期刊名不能被后续某次只有 arXiv 的结果覆盖
                venue = paper.venue
                previous = self.conn.execute(
                    "SELECT venue FROM papers WHERE key = ?", (paper.key,)
                ).fetchone()
                if previous and venue_quality(previous["venue"] or "") > venue_quality(venue or ""):
                    venue = previous["venue"] or ""
                self.conn.execute(
                    """INSERT INTO papers (key, title, authors, year, venue, venue_detail, doi, arxiv_id,
                       url, abstract, item_type, citations, source, sources, extra, first_seen, last_seen,
                       title_norm)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         title=excluded.title,
                         title_norm=excluded.title_norm,
                         authors=excluded.authors,
                         year=COALESCE(excluded.year, papers.year),
                         venue=excluded.venue,
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
                        venue,
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
                        normalize_title(paper.title),
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
    # 高分记忆
    # ------------------------------------------------------------------ #
    def remember(
        self,
        topic: Topic | None,
        recs: Iterable[Recommendation],
        *,
        min_score: float,
        origin: str = "search",
    ) -> list[dict]:
        """把相关度达到 min_score 的论文记入长期记忆。

        规则（保持幂等，重复检索不会产生重复条目）：
          * 新论文 → 新增；
          * 已记住 → 分数取历史最高，空着的概要/理由/特色补齐，不覆盖已有内容；
        返回**本次新增**的条目，方便调用方播报"新记住 N 篇"。
        """
        added: list[dict] = []
        stamp = now_iso()
        with self._lock:
            for rec in recs:
                paper = rec.paper
                if rec.score < min_score or not paper.title:
                    continue
                key = paper.key
                row = self.conn.execute(
                    "SELECT paper_key, score, summary, reason FROM remembered WHERE paper_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        """INSERT INTO remembered (paper_key, topic_id, topic_name, score, score_parts,
                           summary, highlights, reason, note, tags, origin, first_seen, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            key,
                            topic.id if topic else None,
                            topic.name if topic else "",
                            rec.score,
                            _dumps(rec.score_parts),
                            rec.summary,
                            _dumps(rec.highlights),
                            rec.reason,
                            "",
                            _dumps([]),
                            origin,
                            stamp,
                            stamp,
                        ),
                    )
                    added.append(
                        {
                            "key": key,
                            "title": paper.title,
                            "score": round(rec.score, 4),
                            "topic": topic.name if topic else "",
                            "origin": origin,
                        }
                    )
                else:
                    self.conn.execute(
                        """UPDATE remembered SET
                             score = MAX(score, ?),
                             score_parts = ?,
                             topic_id = COALESCE(?, topic_id),
                             topic_name = CASE WHEN ? != '' THEN ? ELSE topic_name END,
                             summary = CASE WHEN summary = '' THEN ? ELSE summary END,
                             highlights = CASE WHEN highlights IN ('[]', '') THEN ? ELSE highlights END,
                             reason = CASE WHEN reason = '' THEN ? ELSE reason END,
                             updated_at = ?
                           WHERE paper_key = ?""",
                        (
                            rec.score,
                            _dumps(rec.score_parts),
                            topic.id if topic else None,
                            topic.name if topic else "",
                            topic.name if topic else "",
                            rec.summary,
                            _dumps(rec.highlights),
                            rec.reason,
                            stamp,
                            key,
                        ),
                    )
            self.conn.commit()
        return added

    def list_remembered(
        self, limit: int = 300, *, min_score: float | None = None, topic_id: int | None = None
    ) -> list[dict]:
        sql = """SELECT m.*, p.title, p.authors, p.year, p.venue, p.doi, p.arxiv_id, p.url,
                        p.abstract, p.citations, p.item_type, p.sources, p.extra,
                        z.zotero_key AS zotero_key
                 FROM remembered m
                 JOIN papers p ON p.key = m.paper_key
                 LEFT JOIN zotero_links z ON z.paper_key = m.paper_key
                 WHERE 1=1"""
        params: list[Any] = []
        if min_score is not None:
            sql += " AND m.score >= ?"
            params.append(min_score)
        if topic_id is not None:
            sql += " AND m.topic_id = ?"
            params.append(topic_id)
        sql += " ORDER BY m.score DESC, m.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_remembered(row) for row in rows]

    def remembered_keys(self, *, min_score: float | None = None) -> set[str]:
        sql = "SELECT paper_key FROM remembered"
        params: list[Any] = []
        if min_score is not None:
            sql += " WHERE score >= ?"
            params.append(min_score)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return {row["paper_key"] for row in rows}

    def get_remembered(self, paper_key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                """SELECT m.*, p.title, p.authors, p.year, p.venue, p.doi, p.arxiv_id, p.url,
                          p.abstract, p.citations, p.item_type, p.sources, p.extra,
                          z.zotero_key AS zotero_key
                   FROM remembered m
                   JOIN papers p ON p.key = m.paper_key
                   LEFT JOIN zotero_links z ON z.paper_key = m.paper_key
                   WHERE m.paper_key = ?""",
                (paper_key,),
            ).fetchone()
        return _row_to_remembered(row) if row else None

    def forget_remembered(self, paper_key: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM remembered WHERE paper_key = ?", (paper_key,))
            self.conn.commit()
        return cur.rowcount > 0

    def update_remembered(self, paper_key: str, *, note: str | None = None, tags: list[str] | None = None) -> bool:
        sets, params = [], []
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if tags is not None:
            sets.append("tags = ?")
            params.append(_dumps(tags))
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.extend([now_iso(), paper_key])
        with self._lock:
            cur = self.conn.execute(f"UPDATE remembered SET {', '.join(sets)} WHERE paper_key = ?", params)
            self.conn.commit()
        return cur.rowcount > 0

    def backfill_remembered(self, min_score: float, *, origin: str = "backfill", limit: int = 500) -> list[dict]:
        """把**历史**推荐里已达阈值的论文补记进来（阈值调低后用它补齐旧记录）。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM recommendations WHERE score >= ?
                   ORDER BY score DESC LIMIT ?""",
                (min_score, limit),
            ).fetchall()
        added: list[dict] = []
        for row in rows:
            paper = self.get_paper(row["paper_key"])
            if not paper:
                continue
            rec = Recommendation(
                paper=paper,
                score=row["score"],
                score_parts=_loads(row["score_parts"], {}),
                summary=row["summary"] or "",
                highlights=_loads(row["highlights"], []),
                reason=row["reason"] or "",
            )
            topic = self.get_topic(row["topic_id"]) if row["topic_id"] else None
            added.extend(self.remember(topic, [rec], min_score=min_score, origin=origin))
        return added

    def remembered_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) AS c FROM remembered").fetchone()["c"])

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
            remembered = self.conn.execute("SELECT COUNT(*) AS c FROM remembered").fetchone()["c"]
            linked = self.conn.execute("SELECT COUNT(*) AS c FROM zotero_links").fetchone()["c"]
        return {
            "papers": papers,
            "topics": topics,
            "recommendations": recs,
            "accepted": accepted,
            "remembered": remembered,
            "zotero_links": linked,
        }


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


def _row_to_remembered(row: sqlite3.Row) -> dict:
    """高分记忆条目：论文元信息 + 卡片 + 记忆元数据，扁平成一个 dict 供 UI / 导出使用。"""
    keys = row.keys()
    return {
        "key": row["paper_key"],
        "title": row["title"],
        "authors": _loads(row["authors"], []),
        "year": row["year"],
        "venue": row["venue"] or "",
        "doi": row["doi"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "url": row["url"] or "",
        "abstract": row["abstract"] or "",
        "citations": row["citations"],
        "item_type": row["item_type"] or "journalArticle",
        "sources": _loads(row["sources"], []),
        "extra": _loads(row["extra"], {}),
        "topic_id": row["topic_id"],
        "topic_name": row["topic_name"] or "",
        "score": row["score"],
        "score_parts": _loads(row["score_parts"], {}),
        "summary": row["summary"] or "",
        "highlights": _loads(row["highlights"], []),
        "reason": row["reason"] or "",
        "note": row["note"] or "",
        "tags": _loads(row["tags"], []),
        "origin": row["origin"] or "",
        "first_seen": row["first_seen"] or "",
        "updated_at": row["updated_at"] or "",
        # 只有带 LEFT JOIN zotero_links 的查询才有这一列
        "zotero_key": (row["zotero_key"] if "zotero_key" in keys else "") or "",
    }
