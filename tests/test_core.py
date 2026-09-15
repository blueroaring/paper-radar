"""离线单元测试：全部只用标准库，不访问网络。

运行：  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_radar import rank, render, textutil
from paper_radar.config import Config, save_secrets
from paper_radar.digest import filter_recent
from paper_radar.engine import title_similarity
from paper_radar.mailer import Mailer
from paper_radar.models import Paper, Recommendation, Seed, Topic, fingerprint, normalize_title
from paper_radar.sources.arxiv import _matches_locally, parse_arxiv_feed, parse_arxiv_rss
from paper_radar.sources.crossref import item_to_paper
from paper_radar.sources.dblp import hit_to_paper
from paper_radar.sources.google_scholar import parse_scholar_html, split_result_blocks
from paper_radar.sources.openalex import rebuild_abstract, work_to_paper
from paper_radar.sources.semantic_scholar import _to_paper
from paper_radar.store import Store
from paper_radar.zotero import paper_to_zotero_item, split_name


def make_cfg(**overrides) -> Config:
    cfg = Config.load()
    for dotted, value in overrides.items():
        cfg.set(dotted.replace("__", "."), value)
    return cfg


class TestConfig(unittest.TestCase):
    def test_defaults_come_from_example(self):
        cfg = Config.load()
        self.assertEqual(cfg.get("sources.enabled")[0], "arxiv")
        self.assertTrue(cfg.get("search.venue_tiers.S"))

    def test_env_override_and_nested_merge(self):
        os.environ["PAPER_RADAR_LLM__MODEL"] = "unit-test-model"
        os.environ["PAPER_RADAR_PORT"] = "9999"
        try:
            cfg = Config.load()
            self.assertEqual(cfg.get("llm.model"), "unit-test-model")
            self.assertEqual(cfg.get("app.port"), 9999)
            self.assertEqual(cfg.get("app.host"), "127.0.0.1")  # 未被覆盖的键仍然来自 example
        finally:
            os.environ.pop("PAPER_RADAR_LLM__MODEL", None)
            os.environ.pop("PAPER_RADAR_PORT", None)

    def test_redaction_hides_secrets(self):
        cfg = Config.load()
        cfg.set("llm.api_key", "sk-secret")
        cfg.set("mail.smtp.password", "pw")
        masked = cfg.as_dict(redact=True)
        self.assertEqual(masked["llm"]["api_key"], "***")
        self.assertEqual(masked["mail"]["smtp"]["password"], "***")

    def test_tolerates_utf8_bom(self):
        # 记事本 / PowerShell 5.1 的 Set-Content -Encoding utf8 都会写 BOM，
        # 而 json.load(encoding="utf-8") 遇到 BOM 会抛 JSONDecodeError。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            payload = json.dumps({"app": {"port": 8123}}, ensure_ascii=False)
            path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
            self.assertEqual(Config.load(path).get("app.port"), 8123)

    def test_bad_json_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text('{"app": {"port": 8848,}}', encoding="utf-8")  # 末尾多余逗号
            with self.assertRaises(SystemExit) as caught:
                Config.load(path)
            self.assertIn("不是合法 JSON", str(caught.exception))


class TestModels(unittest.TestCase):
    def test_fingerprint_priority(self):
        a = fingerprint(doi="10.1/abc", title="X")
        b = fingerprint(doi="https://doi.org/10.1/ABC", title="Y")
        self.assertEqual(a, b)
        self.assertTrue(fingerprint(arxiv_id="2501.01234v2").startswith("arxiv:"))
        # 同一篇 arXiv 论文的不同版本必须视作同一篇
        self.assertEqual(fingerprint(arxiv_id="2501.01234v1"), fingerprint(arxiv_id="2501.01234v3"))
        self.assertEqual(normalize_title("Hello,  World!!"), "hello world")

    def test_merge_fills_missing_fields(self):
        left = Paper(title="T", source="arxiv", sources=["arxiv"], abstract="short")
        right = Paper(
            title="T",
            venue="SIGCOMM 2024",
            doi="10.1/x",
            abstract="a much longer abstract",
            citations=10,
            source="dblp",
            sources=["dblp"],
            authors=["A B"],
        )
        left.merge(right)
        self.assertEqual(left.venue, "SIGCOMM 2024")
        self.assertEqual(left.doi, "10.1/x")
        self.assertEqual(left.abstract, "short")  # 自身已有的字段不被覆盖
        self.assertEqual(left.citations, 10)
        self.assertEqual(left.sources, ["arxiv", "dblp"])
        self.assertEqual(left.authors, ["A B"])


class TestTextUtil(unittest.TestCase):
    def test_extract_keywords_prefers_repeated_phrases(self):
        text = (
            "buffer management in switches. buffer management for data center switches. "
            "buffer management reduces packet loss."
        )
        keywords = textutil.extract_keywords([text], top_k=10)
        self.assertTrue(any("buffer" in k for k in keywords))
        self.assertTrue(any("management" in k for k in keywords))

    def test_extract_json_block_handles_fences(self):
        payload = '```json\n{"a": 1, "b": "中文"}\n```'
        self.assertEqual(textutil.extract_json_block(payload)["b"], "中文")
        self.assertEqual(textutil.extract_json_block('前言 {"a": 2} 后记')["a"], 2)
        self.assertIsNone(textutil.extract_json_block("没有 JSON"))


class TestRanking(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.load()

    def _paper(self, title, **kwargs):
        return Paper(title=title, source="test", sources=["test"], **kwargs)

    def test_dedupe_merges_cross_source(self):
        a = Paper(title="Buffer Management", doi="10.1/a", source="openalex", sources=["openalex"])
        b = Paper(title="buffer   management!!", source="dblp", sources=["dblp"], venue="SIGCOMM")
        out = rank.dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].venue, "SIGCOMM")

    def test_relevance_beats_off_topic(self):
        cfg = self.cfg
        on = self._paper("Preemptive buffer management for on-chip switches", year=2025, citations=5)
        off = self._paper("Deep learning for protein folding", year=2025, citations=500)
        scored = rank.score_papers([on, off], keywords=["buffer management", "on-chip switch"], cfg=cfg)
        self.assertEqual(scored[0].title, on.title)
        self.assertGreater(scored[0].score_parts["relevance"], scored[1].score_parts["relevance"])

    def test_venue_tier_word_boundary(self):
        cfg = self.cfg
        top = self._paper("X", venue="Proceedings of the ACM SIGCOMM 2024 Conference")
        self.assertEqual(rank.venue_tier(top, cfg)[0], "S")
        # "TC" 之类的短缩写不应该误伤无关词
        weird = self._paper("Y", venue="Journal of DCTCP Research")
        self.assertEqual(rank.venue_tier(weird, cfg)[0], "unknown")

    def test_negative_keywords_lower_score(self):
        cfg = self.cfg
        good = self._paper("Buffer management scheduling", year=2025)
        bad = self._paper("Buffer management scheduling for quantum chemistry", year=2025)
        scored = rank.score_papers([good, bad], keywords=["buffer management"], negative_keywords=["quantum"], cfg=cfg)
        self.assertEqual(scored[0].title, good.title)

    def test_filters(self):
        papers = [
            self._paper("A", year=2019, citations=1),
            self._paper("B", year=2025, citations=0),
            self._paper("C", year=2025, citations=50, venue="Sensors"),
        ]
        out = rank.apply_filters(papers, year_from=2024, min_citations=0, exclude_venues=["sensors"])
        self.assertEqual([p.title for p in out], ["B"])


class TestSourcesParsing(unittest.TestCase):
    SCHOLAR_HTML = """
    <div id="gs_res_ccl_mid">
      <div class="gs_r gs_or gs_scl" data-cid="abc" data-rp="0">
        <div class="gs_ri">
          <h3 class="gs_rt"><a href="https://example.org/paper1">Buffer <b>management</b> in switches</a></h3>
          <div class="gs_a">Zhiyu Zhang, Minkun Xue - Proceedings of SIGCOMM, 2024 - example.org</div>
          <div class="gs_rs">We study <b>shared</b> buffer management in data center switches.</div>
          <div class="gs_fl"><a href="#">Cited by 42</a></div>
        </div>
      </div>
      <div class="gs_r gs_or gs_scl" data-cid="def" data-rp="1">
        <div class="gs_ri">
          <h3 class="gs_rt"><span class="gs_ctu">[PDF]</span><a href="https://arxiv.org/abs/2501.00001">Preemptive buffer management</a></h3>
          <div class="gs_a">Hao Li - arXiv preprint, 2025 - arxiv.org</div>
          <div class="gs_rs">On-chip shared memory switches.</div>
        </div>
      </div>
    </div>
    """

    def test_scholar_block_split(self):
        self.assertEqual(len(split_result_blocks(self.SCHOLAR_HTML)), 2)

    def test_scholar_parse_fields(self):
        papers = parse_scholar_html(self.SCHOLAR_HTML)
        self.assertEqual(len(papers), 2)
        first = papers[0]
        self.assertEqual(first.title, "Buffer management in switches")
        self.assertEqual(first.url, "https://example.org/paper1")
        self.assertEqual(first.year, 2024)
        self.assertIn("SIGCOMM", first.venue)
        self.assertEqual(first.citations, 42)
        self.assertEqual(first.authors, ["Zhiyu Zhang", "Minkun Xue"])
        second = papers[1]
        self.assertEqual(second.title, "Preemptive buffer management")
        self.assertEqual(second.item_type, "preprint")

    ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2501.01234v1</id>
        <published>2025-01-02T18:00:00Z</published>
        <title>Occamy: Preemptive Buffer Management
        for On-chip Switches</title>
        <summary>  We present a preemptive buffer manager.  </summary>
        <author><name>Hao Li</name></author>
        <author><name>Danfeng Shan</name></author>
        <link href="http://arxiv.org/abs/2501.01234v1" rel="alternate" type="text/html"/>
        <arxiv:primary_category term="cs.NI"/>
        <arxiv:comment>15 pages</arxiv:comment>
      </entry>
    </feed>"""

    def test_arxiv_parse(self):
        papers = parse_arxiv_feed(self.ARXIV_XML)
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p.arxiv_id, "2501.01234v1")
        self.assertEqual(p.year, 2025)
        self.assertEqual(p.title, "Occamy: Preemptive Buffer Management for On-chip Switches")
        self.assertEqual(p.abstract, "We present a preemptive buffer manager.")
        self.assertEqual(p.authors, ["Hao Li", "Danfeng Shan"])
        self.assertEqual(p.item_type, "preprint")
        self.assertIn("cs.NI", p.venue)
        self.assertEqual(parse_arxiv_feed("not xml")[:], [])

    def test_openalex_abstract_and_work(self):
        abstract = rebuild_abstract({"Buffer": [0], "management": [1], "works": [2]})
        self.assertEqual(abstract, "Buffer management works")
        work = {
            "title": "OBM: Optimal Shared Packet Buffer Management",
            "doi": "https://doi.org/10.1145/1.2",
            "publication_year": 2026,
            "type": "proceedings-article",
            "cited_by_count": 3,
            "authorships": [{"author": {"display_name": "Dan Mani Binu"}}],
            "primary_location": {"source": {"display_name": "SIGCOMM"}, "landing_page_url": "https://x.org"},
            "abstract_inverted_index": {"hello": [0], "world": [1]},
            "primary_topic": {"display_name": "Networking"},
        }
        paper = work_to_paper(work)
        self.assertEqual(paper.doi, "10.1145/1.2")
        self.assertEqual(paper.item_type, "conferencePaper")
        self.assertEqual(paper.abstract, "hello world")
        self.assertEqual(paper.venue, "SIGCOMM")

    def test_dblp_hit(self):
        hit = {
            "info": {
                "title": "Themis: Scheduling-Aware Buffer Management.",
                "venue": "SIGCOMM",
                "year": "2025",
                "type": "Conference and Workshop Papers",
                "doi": "10.1145/3.4",
                "ee": "https://doi.org/10.1145/3.4",
                "authors": {"author": [{"text": "Zhiyu Zhang"}, {"text": "Yang Xu"}]},
            }
        }
        paper = hit_to_paper(hit)
        self.assertEqual(paper.venue, "SIGCOMM")
        self.assertEqual(paper.year, 2025)
        self.assertEqual(paper.item_type, "conferencePaper")
        self.assertEqual(paper.authors, ["Zhiyu Zhang", "Yang Xu"])

    def test_semantic_scholar_item(self):
        item = {
            "title": "A Buffer Paper",
            "year": 2024,
            "venue": "NSDI",
            "citationCount": 7,
            "externalIds": {"DOI": "10.1/z", "ArXiv": "2401.00001"},
            "authors": [{"name": "A B"}],
            "publicationTypes": ["Conference"],
        }
        paper = _to_paper(item)
        self.assertEqual(paper.item_type, "conferencePaper")
        self.assertEqual(paper.arxiv_id, "2401.00001")

    ARXIV_RSS = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel>
        <item>
          <title>Preemptive Buffer Management for On-chip Switches</title>
          <link>https://arxiv.org/abs/2609.13795</link>
          <description>arXiv:2609.13795v1 Announce Type: new
Abstract: We present a preemptive buffer manager for on-chip shared-memory switches.</description>
          <guid isPermaLink="false">oai:arXiv.org:2609.13795v1</guid>
          <category>cs.NI</category>
          <pubDate>Tue, 15 Sep 2026 00:00:00 -0400</pubDate>
          <arxiv:announce_type>new</arxiv:announce_type>
          <arxiv:DOI>10.1145/3795866.3844758</arxiv:DOI>
          <dc:creator>Enguang Fan, Binh Minh Tran</dc:creator>
        </item>
      </channel>
    </rss>"""

    def test_arxiv_rss_parse(self):
        papers = parse_arxiv_rss(self.ARXIV_RSS)
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p.arxiv_id, "2609.13795")
        self.assertEqual(p.year, 2026)
        self.assertEqual(p.authors, ["Enguang Fan", "Binh Minh Tran"])
        self.assertEqual(p.doi, "10.1145/3795866.3844758")
        self.assertIn("preemptive buffer manager", p.abstract)
        self.assertEqual(p.extra["channel"], "rss")
        self.assertNotIn("Announce Type", p.abstract)

    def test_arxiv_rss_local_filter(self):
        papers = parse_arxiv_rss(self.ARXIV_RSS)
        self.assertTrue(_matches_locally(papers[0], '"buffer management" AND switch'))
        self.assertFalse(_matches_locally(papers[0], "quantum chemistry protein folding"))

    def test_crossref_item(self):
        item = {
            "title": ["Revisiting Preemptive Buffer Management"],
            "DOI": "10.1109/tc.2026.3711067",
            "type": "proceedings-article",
            "issued": {"date-parts": [[2026, 3, 1]]},
            "container-title": ["IEEE Transactions on Computers"],
            "author": [{"given": "Danfeng", "family": "Shan"}],
            "is-referenced-by-count": 4,
            "abstract": "<jats:p>An <jats:bold>abstract</jats:bold> here.</jats:p>",
            "URL": "https://doi.org/10.1109/tc.2026.3711067",
        }
        paper = item_to_paper(item)
        self.assertEqual(paper.year, 2026)
        self.assertEqual(paper.venue, "IEEE Transactions on Computers")
        self.assertEqual(paper.item_type, "conferencePaper")
        self.assertEqual(paper.authors, ["Danfeng Shan"])
        self.assertEqual(paper.abstract, "An abstract here.")
        self.assertEqual(paper.citations, 4)
        self.assertIsNone(item_to_paper({"title": [], "type": "component"}))

    def test_crossref_event_as_venue(self):
        item = {
            "title": ["A Paper"],
            "type": "proceedings-article",
            "event": {"name": "ACM SIGCOMM 2026"},
            "issued": {"date-parts": [[None]]},
        }
        paper = item_to_paper(item)
        self.assertEqual(paper.venue, "ACM SIGCOMM 2026")
        self.assertIsNone(paper.year)


class TestZoteroPayload(unittest.TestCase):
    def test_split_name(self):
        self.assertEqual(split_name("Zhiyu Zhang")["lastName"], "Zhang")
        self.assertEqual(split_name("Zhang, Zhiyu")["firstName"], "Zhiyu")
        self.assertEqual(split_name("张三")["lastName"], "张三")

    def test_item_mapping_by_type(self):
        conf = Paper(title="T", item_type="conferencePaper", venue="SIGCOMM", year=2024, authors=["A B"], doi="10.1/x")
        item = paper_to_zotero_item(conf, collections=["C1"], tags=["paper-radar"])
        self.assertEqual(item["itemType"], "conferencePaper")
        self.assertEqual(item["proceedingsTitle"], "SIGCOMM")
        self.assertEqual(item["date"], "2024")
        self.assertEqual(item["collections"], ["C1"])
        self.assertEqual(item["tags"], [{"tag": "paper-radar"}])
        self.assertEqual(item["creators"][0]["creatorType"], "author")

        pre = Paper(title="P", item_type="preprint", arxiv_id="2501.1", year=2025)
        item = paper_to_zotero_item(pre)
        self.assertEqual(item["repository"], "arXiv")
        self.assertEqual(item["archiveID"], "arXiv:2501.1")

        mapping = paper_to_zotero_item(conf, item_type_map={"conferencePaper": "journalArticle"})
        self.assertEqual(mapping["itemType"], "journalArticle")
        self.assertIn("publicationTitle", mapping)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_topic_roundtrip(self):
        topic = Topic(
            name="交换机BM",
            direction="buffer management",
            seeds=[Seed(value="10.1/x", title="X")],
            keywords=["buffer"],
            queries=["buffer management"],
            filters={"exclude_venues": ["Sensors"]},
        )
        topic.id = self.store.save_topic(topic)
        loaded = self.store.get_topic(topic.id)
        self.assertEqual(loaded.name, "交换机BM")
        self.assertEqual(loaded.keywords, ["buffer"])
        self.assertEqual(loaded.seeds[0].title, "X")
        self.assertEqual(loaded.filters["exclude_venues"], ["Sensors"])
        self.assertEqual(self.store.find_topic_by_name("交换机BM").id, topic.id)
        self.store.delete_topic(topic.id)
        self.assertIsNone(self.store.get_topic(topic.id))

    def test_new_paper_tracking(self):
        topic = Topic(name="t")
        topic.id = self.store.save_topic(topic)
        paper = Paper(title="A paper", source="test", sources=["test"])
        self.store.upsert_papers([paper])
        self.assertTrue(self.store.is_new_for_topic(topic.id, paper.key))
        self.store.save_recommendations(
            topic.id, [Recommendation(paper=paper, score=0.5, summary="s", reason="r")], status="sent"
        )
        self.assertFalse(self.store.is_new_for_topic(topic.id, paper.key))
        rows = self.store.list_recommendations(topic.id)
        self.assertEqual(rows[0]["summary"], "s")
        self.assertEqual(rows[0]["status"], "sent")
        self.store.set_status(topic.id, paper.key, "accepted")
        self.assertEqual(self.store.list_recommendations(topic.id)[0]["status"], "accepted")

    def test_run_logging_and_stats(self):
        run = self.store.start_run("digest", 1)
        self.store.finish_run(run, status="ok", stats={"new": 3}, report_path="x.html")
        runs = self.store.recent_runs()
        self.assertEqual(runs[0]["stats"]["new"], 3)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(self.store.stats()["topics"], 0)

    def test_zotero_link_is_idempotent(self):
        # 对应真实坑：Zotero 搜索索引有延迟，刚写入的条目远端查重查不到，
        # 本地链接表负责让「连点两次加入 Zotero」只写一次。
        self.assertFalse(self.store.is_linked_zotero("k1"))
        self.store.link_zotero("k1", "ZOTKEY", "标题")
        self.assertTrue(self.store.is_linked_zotero("k1"))
        self.assertEqual(self.store.zotero_links()["k1"]["zotero_key"], "ZOTKEY")
        self.store.link_zotero("k1", "ZOTKEY2", "标题2")
        self.assertEqual(len(self.store.zotero_links()), 1)
        self.assertEqual(self.store.zotero_links()["k1"]["zotero_key"], "ZOTKEY2")


class TestRender(unittest.TestCase):
    def test_table_contains_three_key_columns(self):
        rec = Recommendation(
            paper=Paper(title="T1", year=2024, venue="SIGCOMM", url="https://x.org", doi="10.1/x"),
            score=0.87,
            summary="它做了什么",
            highlights=["特色一", "特色二"],
            reason="因为相关",
        )
        html = render.render_digest(
            topic_name="交换机BM",
            direction="buffer management",
            items=[rec],
            generated_at="2026-01-01 08:30",
            sources_used=["arxiv", "dblp"],
        )
        for token in ("内容概要", "文章特色", "推荐理由", "发表在哪里", "SIGCOMM", "它做了什么", "特色一", "因为相关", "87"):
            self.assertIn(token, html)

    def test_text_render(self):
        rec = Recommendation(paper=Paper(title="T2", year=2024), score=0.5, summary="S", reason="R")
        text = render.render_text([rec], topic_name="方向")
        self.assertIn("T2", text)
        self.assertIn("概要: S", text)
        self.assertIn("推荐理由: R", text)


class TestMailer(unittest.TestCase):
    def test_build_message_and_file_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load()
            cfg.set("app.data_dir", tmp)
            cfg.set("mail.enabled", True)
            cfg.set("mail.transport", "file")
            cfg.set("mail.smtp.username", "me@example.com")
            cfg.set("mail.smtp.from_name", "Paper Radar")
            cfg.set("mail.to", ["you@example.com"])
            mailer = Mailer(cfg)
            self.assertTrue(mailer.enabled())
            result = mailer.send(subject="[Paper Radar] 测试", html="<p>hi</p>", text="hi")
            self.assertTrue(result["ok"], result)
            self.assertTrue(Path(result["path"]).exists())
            content = Path(result["path"]).read_text(encoding="utf-8", errors="replace")
            self.assertIn("you@example.com", content)

    def test_disabled_when_no_recipient(self):
        cfg = Config.load()
        cfg.set("mail.enabled", True)
        cfg.set("mail.to", [])
        self.assertFalse(Mailer(cfg).enabled())

    def test_secrets_written_separately(self):
        # 用临时路径替代真实 secrets.json，测试绝不触碰本机密钥
        import paper_radar.config as cfgmod

        with tempfile.TemporaryDirectory() as tmp:
            original = cfgmod.SECRETS_PATH
            cfgmod.SECRETS_PATH = Path(tmp) / "secrets.json"
            try:
                save_secrets({"llm": {"api_key": "unit-test"}})
                self.assertIn("unit-test", cfgmod.SECRETS_PATH.read_text(encoding="utf-8"))
                self.assertEqual(Config.load().get("llm.api_key"), "unit-test")
            finally:
                cfgmod.SECRETS_PATH = original
                Config.load()


class TestDigestHelpers(unittest.TestCase):
    def test_filter_recent_uses_published_then_year(self):
        fresh = Paper(title="fresh", year=2026, extra={"published": "2026-09-01T00:00:00Z"})
        old = Paper(title="old", year=2019)
        no_year = Paper(title="no-year")
        out = filter_recent([fresh, old, no_year], lookback_days=45)
        titles = [p.title for p in out]
        self.assertIn("fresh", titles)
        self.assertNotIn("old", titles)
        self.assertIn("no-year", titles)


class TestTitleSimilarity(unittest.TestCase):
    def test_gate_rejects_unrelated_fuzzy_match(self):
        original = "Themis: Scheduling-Aware Buffer Management for HBM-Based Hybrid Buffers"
        self.assertGreater(title_similarity(original, original), 0.99)
        self.assertLess(title_similarity(original, "HSM"), 0.5)
        self.assertLess(title_similarity("Occamy: A Preemptive Buffer Management", "Ocean: a buffer management"), 0.85)


class _FakeCtx:
    def __init__(self, cfg):
        self.cfg = cfg


class TestScheduler(unittest.TestCase):
    """覆盖真实踩过的坑：晚上启动控制台不该立刻补发早报。"""

    def _scheduler(self, cfg):
        import paper_radar.scheduler as sched

        return sched.DailyScheduler(_FakeCtx(cfg))

    def test_skips_when_catch_up_window_exceeded(self):
        import paper_radar.scheduler as sched

        cfg = Config.load()
        cfg.set("digest.enabled", True)
        cfg.set("digest.days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        cfg.set("digest.time", "00:00")
        cfg.set("digest.catch_up_window_minutes", 1)
        called = []
        original = sched.run_digest
        sched.run_digest = lambda *a, **k: called.append(1) or {}
        try:
            scheduler = sched.DailyScheduler(_FakeCtx(cfg))
            scheduler._tick()
            scheduler._tick()
            self.assertEqual(called, [], "超出补跑窗口时不应触发")
        finally:
            sched.run_digest = original

    def test_fires_at_target_time(self):
        import paper_radar.scheduler as sched
        from datetime import datetime

        cfg = Config.load()
        cfg.set("digest.enabled", True)
        cfg.set("digest.days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        cfg.set("digest.time", datetime.now().strftime("%H:%M"))
        cfg.set("digest.catch_up_window_minutes", 180)
        called = []
        original = sched.run_digest
        sched.run_digest = lambda *a, **k: called.append(1) or {}
        try:
            scheduler = sched.DailyScheduler(_FakeCtx(cfg))
            scheduler._tick()
            scheduler._tick()  # 同一分钟只触发一次
            self.assertEqual(len(called), 1)
            self.assertFalse(scheduler.status()["running_now"])
        finally:
            sched.run_digest = original


class TestLLMRelevance(unittest.TestCase):
    def test_score_relevance_parsing(self):
        from paper_radar.summarize import LLMClient, TaskResult

        cfg = Config.load()
        cfg.set("llm.enabled_for.relevance", True)
        client = LLMClient(cfg, None)
        papers = [Paper(title="A"), Paper(title="B")]
        client.complete = lambda *a, **k: TaskResult(
            {"scores": [{"id": 1, "score": 9}, {"id": 2, "score": 2}]}, provider="fake"
        )
        scores = client.score_relevance(papers, direction="buffer", keywords=["buffer"])
        self.assertAlmostEqual(scores[papers[0].key], 0.9)
        self.assertAlmostEqual(scores[papers[1].key], 0.2)

    def test_rerank_blends_scores(self):
        from paper_radar.engine import Context, Engine

        cfg = Config.load()
        cfg.set("llm.enabled_for.relevance", True)
        cfg.set("search.llm_relevance_weight", 0.5)
        with tempfile.TemporaryDirectory() as tmp:
            cfg.set("app.data_dir", tmp)
            ctx = Context(cfg)
            engine = Engine(ctx)
            good = Paper(title="on topic", source="t", sources=["t"])
            bad = Paper(title="off topic", source="t", sources=["t"])
            good.score, bad.score = 0.4, 0.6
            ctx.llm.score_relevance = lambda papers, **k: {
                good.key: 1.0,
                bad.key: 0.0,
            }
            ranked = engine.rerank_with_llm(Topic(name="x"), [bad, good])
            self.assertEqual(ranked[0].title, "on topic")
            self.assertAlmostEqual(ranked[0].score, 0.7)
            ctx.store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
