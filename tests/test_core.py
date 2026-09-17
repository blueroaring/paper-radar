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
from paper_radar.models import (
    Paper,
    Recommendation,
    Seed,
    Topic,
    fingerprint,
    normalize_title,
    venue_quality,
)
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

    def test_venue_quality_and_merge_prefers_formal_venue(self):
        # 「发表在哪里」是推荐表的核心列，不能被 arXiv 占位名压过去
        self.assertEqual(venue_quality(""), 0)
        self.assertEqual(venue_quality("arXiv preprint (cs.NI)"), 1)
        self.assertEqual(venue_quality("arXiv (Cornell University)"), 1)
        self.assertEqual(venue_quality("Proceedings of the Twentieth European Conference on Computer Systems"), 2)

        pre = Paper(title="Occamy", venue="arXiv (Cornell University)", source="openalex", sources=["openalex"])
        conf = Paper(title="Occamy", venue="EuroSys 2025", doi="10.1/o", source="crossref", sources=["crossref"])
        pre.merge(conf)
        self.assertEqual(pre.venue, "EuroSys 2025", "正式会议名应替换预印本占位名")

        # 反向：正式 venue 在前，不该被预印本名覆盖
        conf2 = Paper(title="Occamy", venue="EuroSys 2025", source="crossref", sources=["crossref"])
        conf2.merge(Paper(title="Occamy", venue="arXiv preprint", source="arxiv", sources=["arxiv"]))
        self.assertEqual(conf2.venue, "EuroSys 2025")


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


class TestMailDeliveryRobustness(unittest.TestCase):
    """邮件发失败时不能把论文标成"已推送"，否则用户永远收不到那几篇。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")
        self.topic = Topic(name="交换机BM")
        self.topic.id = self.store.save_topic(self.topic)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _recs(self, *titles):
        recs = []
        for title in titles:
            paper = Paper(title=title, doi=f"10.1/{title}", source="t", sources=["t"])
            self.store.upsert_papers([paper])
            recs.append(Recommendation(paper=paper, score=0.7))
        return recs

    def test_failed_send_keeps_papers_retryable(self):
        """关键回归：发信失败后，这些论文必须还能被下次简报重新捡起来。

        踩过的坑：先标 sent 再发信 → 一次网络故障就永久吞掉那几篇；
        后来改成退回 shown 也不够 —— only_new 看的是"有没有记录"，
        有记录就不再算新论文，于是既不会重发也永远不会被清理。
        正确做法是引入 pending（待推送）状态，并让简报按它筛候选。
        """
        from paper_radar.digest import _finalize_mail_status

        recs = self._recs("A", "B")
        self.store.save_recommendations(self.topic.id, recs, status="pending")
        logs: list[str] = []
        ctx = type("Ctx", (), {"store": self.store})()

        ok = _finalize_mail_status(
            ctx, self.topic, recs, {"ok": False, "error": "SSLEOFError", "hint": "代理可能拦了 SMTP"}, logs.append
        )
        self.assertFalse(ok)
        statuses = {r["status"] for r in self.store.list_recommendations(self.topic.id)}
        self.assertEqual(statuses, {"pending"})
        self.assertTrue(any("保持待推送" in line for line in logs))
        self.assertTrue(any("排查提示" in line for line in logs))

        # 下次简报必须能重新捡起这两篇
        papers = [r.paper for r in recs]
        self.assertEqual(len(self.store.filter_digest_pending(self.topic.id, papers)), 2)
        self.assertEqual(len(self.store.filter_new(self.topic.id, papers)), 0, "filter_new 仍视其为已记录")

    def test_successful_send_marks_sent_and_wont_resend(self):
        from paper_radar.digest import _finalize_mail_status

        recs = self._recs("C", "D")
        self.store.save_recommendations(self.topic.id, recs, status="pending")
        ctx = type("Ctx", (), {"store": self.store})()
        ok = _finalize_mail_status(ctx, self.topic, recs, {"ok": True}, lambda _m: None)
        self.assertTrue(ok)
        statuses = {r["status"] for r in self.store.list_recommendations(self.topic.id)}
        self.assertEqual(statuses, {"sent"})
        papers = [r.paper for r in recs]
        self.assertEqual(self.store.filter_digest_pending(self.topic.id, papers), [], "发过的不能再发")

    def test_digest_pending_ignores_manually_shown(self):
        """手动检索看过的（shown）不该再被邮件打扰。"""
        recs = self._recs("E")
        self.store.save_recommendations(self.topic.id, recs, status="shown")
        self.assertEqual(self.store.filter_digest_pending(self.topic.id, [recs[0].paper]), [])

    def test_dropped_status_stops_reevaluation(self):
        """被阈值刷掉的候选标成 dropped 后，不该再进候选 —— 否则每天白评一遍。"""
        recs = self._recs("G")
        self.store.save_recommendations(self.topic.id, recs, status="pending")
        self.assertEqual(len(self.store.filter_digest_pending(self.topic.id, [recs[0].paper])), 1)
        self.store.set_status(self.topic.id, recs[0].paper.key, "dropped")
        self.assertEqual(self.store.filter_digest_pending(self.topic.id, [recs[0].paper]), [])

    def test_mark_judged_records_first_time_candidates(self):
        """首次出现就被刷掉的候选必须留下记录，否则下次仍算"新论文"，每天重评。"""
        p1 = Paper(title="首次出现但不够相关", doi="10.1/x1", source="t", sources=["t"])
        p2 = Paper(title="已有记录", doi="10.1/x2", source="t", sources=["t"])
        self.store.upsert_papers([p1, p2])
        # p2 已有一条带卡片内容的 pending 记录
        self.store.save_recommendations(
            self.topic.id,
            [Recommendation(paper=p2, score=0.5, summary="保留我", reason="R")],
            status="pending",
        )
        created = self.store.mark_judged(self.topic.id, [p1, p2], status="dropped")
        self.assertEqual(created, 1, "只有首次出现的那个才新建记录")
        # 下次都不再是候选
        self.assertEqual(self.store.filter_digest_pending(self.topic.id, [p1, p2]), [])
        rows = {r["key"]: r for r in self.store.list_recommendations(self.topic.id)}
        self.assertEqual(rows[p2.key]["summary"], "保留我", "已有卡片内容不能被覆盖")
        self.store.set_status(self.topic.id, p1.key, "dropped")
        self.assertEqual({r["status"] for r in self.store.list_recommendations(self.topic.id)}, {"dropped"})

    def test_set_status_reports_zero_rows_on_key_mismatch(self):
        """key 对不上时必须能看见（返回 0），而不是静默成功。"""
        recs = self._recs("H")
        self.store.save_recommendations(self.topic.id, recs, status="pending")
        self.assertEqual(self.store.set_status(self.topic.id, recs[0].paper.key, "dropped"), 1)
        self.assertEqual(self.store.set_status(self.topic.id, "doi:不存在的键", "dropped"), 0)
        # 按标题定位则不受指纹变化影响
        self.assertEqual(self.store.set_status_by_title(self.topic.id, recs[0].paper.title, "shown"), 1)
        self.assertEqual(self.store.list_recommendations(self.topic.id)[0]["status"], "shown")

    def test_requeue_failed_mail_sets_pending(self):
        """requeue 命令必须把状态设成 pending，否则退回 shown 等于没退（不会被重发）。"""
        recs = self._recs("F")
        self.store.save_recommendations(self.topic.id, recs, status="sent")
        run_id = self.store.start_run("digest", self.topic.id)
        self.store.finish_run(
            run_id,
            status="ok",
            stats={
                "topic_id": self.topic.id,
                "mail": {"ok": False, "error": "SSLEOFError"},
                "items": [{"key": recs[0].paper.key, "title": "F"}],
            },
        )
        result = self.store.requeue_failed_mail()
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(self.store.list_recommendations(self.topic.id)[0]["status"], "pending")
        self.assertEqual(len(self.store.filter_digest_pending(self.topic.id, [recs[0].paper])), 1)
        # 成功的运行不该被动到
        self.assertEqual(self.store.requeue_failed_mail()["requeued"], 0)

    def test_smtp_endpoints_include_fallback_port(self):
        from paper_radar.mailer import SmtpTransport

        cfg = Config.load()
        cfg.set("mail.smtp.port", 465)
        cfg.set("mail.smtp.security", "ssl")
        self.assertEqual(SmtpTransport(cfg)._endpoints(), [(465, "ssl"), (587, "starttls")])

        cfg.set("mail.smtp.port", 587)
        cfg.set("mail.smtp.security", "starttls")
        self.assertEqual(SmtpTransport(cfg)._endpoints(), [(587, "starttls"), (465, "ssl")])

    def test_smtp_falls_back_to_second_endpoint(self):
        from email.message import EmailMessage

        from paper_radar.mailer import SmtpTransport

        cfg = Config.load()
        cfg.set("mail.smtp.host", "smtp.example.com")
        cfg.set("mail.smtp.port", 465)
        cfg.set("mail.retries", 1)
        transport = SmtpTransport(cfg)
        tried: list[tuple[int, str]] = []

        def fake_attempt(host, port, security, message):
            tried.append((port, security))
            if (port, security) == (465, "ssl"):
                raise TimeoutError("模拟主端点被拦")

        transport._attempt = fake_attempt  # type: ignore[assignment]
        result = transport.send(EmailMessage())
        self.assertTrue(result["ok"], "主端点失败后应由备用端点顶上")
        self.assertEqual(tried, [(465, "ssl"), (587, "starttls")])
        self.assertIn("note", result)

    def test_smtp_reports_hint_when_all_endpoints_fail(self):
        from email.message import EmailMessage

        from paper_radar.mailer import SMTP_HINT, SmtpTransport

        cfg = Config.load()
        cfg.set("mail.smtp.host", "smtp.example.com")
        cfg.set("mail.retries", 1)
        transport = SmtpTransport(cfg)

        def boom(*_a, **_k):
            raise TimeoutError("模拟全挂")

        transport._attempt = boom  # type: ignore[assignment]
        result = transport.send(EmailMessage())
        self.assertFalse(result["ok"])
        self.assertEqual(result["hint"], SMTP_HINT)
        self.assertIn("TUN", result["hint"])


class TestExcludeKnownPapers(unittest.TestCase):
    """用户已经知道的论文（方向种子 / 已入过 Zotero）不能再被推荐或发进简报。"""

    def setUp(self):
        self.topic = Topic(
            name="交换机BM",
            seeds=[
                Seed(value="Themis: Scheduling-Aware Buffer Management for HBM-Based Hybrid Buffers"),
                Seed(value="10.1145/3689031.3717495", title="Occamy"),
                Seed(value="arXiv:2501.13570"),
            ],
        )

    def test_seed_index_extracts_ids_and_titles(self):
        from paper_radar.rank import seed_index

        index = seed_index(self.topic.seeds)
        self.assertIn("10.1145/3689031.3717495", index["doi"])
        self.assertIn("2501.13570", index["arxiv"])
        self.assertIn(
            "themis scheduling aware buffer management for hbm based hybrid buffers", index["title"]
        )

    def test_doi_digits_are_not_mistaken_for_arxiv_id(self):
        """真实误判：10.1145/3689031.3717495 里的 "9031.37174" 曾被当成 arXiv ID。"""
        from paper_radar.rank import seed_index

        index = seed_index([Seed(value="10.1145/3689031.3717495")])
        self.assertEqual(index["arxiv"], set(), "DOI 数字不该被解析成 arXiv ID")

    def test_seed_matched_by_title_doi_and_arxiv(self):
        from paper_radar.rank import is_seed_paper, seed_index

        index = seed_index(self.topic.seeds)
        # 标题在库里可能带着花括号/大小写差异，归一化后应能命中
        self.assertTrue(
            is_seed_paper(
                Paper(title="Themis:{Scheduling-Aware} Buffer Management for {HBM-Based} Hybrid Buffers"), index
            )
        )
        self.assertTrue(is_seed_paper(Paper(title="Occamy", doi="10.1145/3689031.3717495"), index))
        self.assertTrue(is_seed_paper(Paper(title="X", arxiv_id="2501.13570v2"), index))
        self.assertFalse(is_seed_paper(Paper(title="An Unrelated Optics Paper"), index))

    def test_engine_excludes_seeds_and_zotero_linked(self):
        from paper_radar.engine import Context, Engine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load()
            cfg.set("app.data_dir", tmp)
            ctx = Context(cfg)
            try:
                engine = Engine(ctx)
                topic = Topic(name="t", seeds=[Seed(value="10.1/seed", title="Seed Paper Title")])
                topic.id = ctx.store.save_topic(topic)
                seed_paper = Paper(title="Seed Paper Title", doi="10.1/seed", source="t", sources=["t"])
                added = Paper(title="Already Added", doi="10.1/added", source="t", sources=["t"])
                fresh = Paper(title="Brand New Work", doi="10.1/new", source="t", sources=["t"])
                ctx.store.upsert_papers([seed_paper, added, fresh])
                ctx.store.link_zotero(added.key, "ZOTKEY", added.title)

                kept, excluded = engine.exclude_known(topic, [seed_paper, added, fresh])
                self.assertEqual([p.title for p in kept], ["Brand New Work"])
                self.assertEqual(excluded, 2)

                # 关掉开关就该原样返回
                cfg.set("search.exclude_seeds", False)
                cfg.set("search.exclude_already_added", False)
                kept, excluded = engine.exclude_known(topic, [seed_paper, added, fresh])
                self.assertEqual(len(kept), 3)
                self.assertEqual(excluded, 0)
            finally:
                ctx.store.close()


class TestStaticRoutes(unittest.TestCase):
    """报告文件名带中文，浏览器发的是百分号编码 —— 不解码就 404（真实故障）。"""

    def test_resolves_percent_encoded_chinese_report_name(self):
        import urllib.parse

        from paper_radar.server import resolve_static_target

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load()
            cfg.set("app.data_dir", tmp)
            name = "2026-09-17-1053-交换机缓冲管理.html"
            report = cfg.reports_dir() / name
            report.write_text("<html>ok</html>", encoding="utf-8")

            # 浏览器实际发出的就是这种编码形式
            encoded = "/reports/" + urllib.parse.quote(name)
            self.assertIn("%E4%BA%A4", encoded)
            self.assertEqual(resolve_static_target(cfg, encoded), report)
            # 未编码的原始形式也应能解析
            self.assertEqual(resolve_static_target(cfg, "/reports/" + name), report)
            # 不存在的报告返回 None（调用方据此返回 404）
            self.assertIsNone(resolve_static_target(cfg, "/reports/nope.html"))

    def test_path_traversal_is_neutralized(self):
        from paper_radar.server import resolve_static_target

        cfg = Config.load()
        # basename 只取最后一段，穿越路径解析不到文件
        self.assertIsNone(resolve_static_target(cfg, "/reports/../../../Windows/System32/drivers/etc/hosts"))
        self.assertIsNone(resolve_static_target(cfg, "/..%2f..%2fsecret.txt"))


class TestLineageMode(unittest.TestCase):
    """脉络模式：从奠基工作出发、按年份往下，而不是被近几年淹没。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "l.db")
        self.topic = Topic(name="交换机BM", keywords=["buffer", "switch", "threshold"])
        self.topic.id = self.store.save_topic(self.topic)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _paper(self, year, title, relevance=0.6, citations=0, origin=False):
        p = Paper(
            title=title,
            doi=f"10.1/{title}",
            year=year,
            citations=citations,
            source="t",
            sources=["t"],
        )
        p.score_parts = {"relevance": relevance}
        p.score = relevance
        if origin:
            p.extra["is_origin"] = True
        return p

    def _engine(self):
        from paper_radar.engine import Context, Engine

        cfg = Config.load()
        cfg.set("app.data_dir", self.tmp.name)
        ctx = Context(cfg)
        return ctx, Engine(ctx)

    def test_timeline_is_chronological_and_pins_origin(self):
        ctx, engine = self._engine()
        try:
            origin = self._paper(1998, "Dynamic Queue Length Thresholds", relevance=0.1, origin=True)
            papers = [
                self._paper(2025, "Recent Work A", relevance=0.9),
                self._paper(2005, "Old Work B", relevance=0.5),
                self._paper(2015, "Mid Work C", relevance=0.7),
            ]
            timeline = engine.build_timeline(self.topic, papers, [origin], limit=10)
            years = [p.year for p in timeline]
            self.assertEqual(years, sorted(years), "必须按年份升序")
            self.assertEqual(years[0], 1998, "奠基工作钉在最前面")
            self.assertTrue(timeline[0].extra.get("is_origin"))
        finally:
            ctx.store.close()

    def test_timeline_balances_years_instead_of_taking_newest(self):
        """每代都取代表：只有近几年高分时，老年代的代表也不该被挤掉。"""
        ctx, engine = self._engine()
        try:
            papers = [
                self._paper(1999, "Very Old", relevance=0.30),
                self._paper(2005, "Old", relevance=0.35),
                self._paper(2015, "Mid", relevance=0.40),
                # 近几年一堆高分
                *[self._paper(2023 + i % 3, f"Recent {i}", relevance=0.95) for i in range(6)],
            ]
            timeline = engine.build_timeline(self.topic, papers, [], limit=8)
            decades = {p.year // 10 * 10 for p in timeline}
            self.assertIn(1990, decades, "1990s 不该被近几年挤掉")
            self.assertIn(2000, decades)
            self.assertIn(2010, decades)
            self.assertLessEqual(len(timeline), 8)
        finally:
            ctx.store.close()

    def test_timeline_filters_off_topic(self):
        ctx, engine = self._engine()
        try:
            papers = [
                self._paper(2000, "On Topic Old", relevance=0.6),
                self._paper(2001, "Off Topic Old", relevance=0.02),
            ]
            timeline = engine.build_timeline(self.topic, papers, [], limit=10)
            self.assertEqual([p.title for p in timeline], ["On Topic Old"])
        finally:
            ctx.store.close()

    def test_manual_origins_take_priority_and_skip_llm(self):
        """用户手填的起点优先，且不该再去问模型。"""
        ctx, engine = self._engine()
        try:
            self.topic.filters = {"lineage_origins": ["Dynamic Queue Length Thresholds"]}
            resolved = Paper(title="Dynamic Queue Length Thresholds", doi="10.1/dt", year=1998)
            engine._paper_by_title = lambda title, threshold=None: resolved  # type: ignore[assignment]
            called = {"llm": 0}

            def boom(**kwargs):
                called["llm"] += 1
                return {"origins": []}

            ctx.llm.propose_origins = boom  # type: ignore[assignment]
            origins = engine.find_origins(self.topic)
            self.assertEqual([o.title for o in origins], ["Dynamic Queue Length Thresholds"])
            self.assertEqual(called["llm"], 0, "手填起点时不应调用模型")
            self.assertEqual(origins[0].extra["origin_source"], "manual")
        finally:
            ctx.store.close()

    def test_llm_origin_must_pass_topicality_gate(self):
        """模型提名要过两道闸门：存在性 + 切题（实测把 TCP/ATM 拥塞控制当成了共享缓冲管理的奠基）。"""
        ctx, engine = self._engine()
        try:
            resolved = Paper(
                title="Dynamics of TCP traffic over ATM networks",
                doi="10.1/tcp",
                year=1995,
                abstract="TCP congestion control over ATM networks.",
            )
            engine._paper_by_title = lambda title, threshold=None: resolved  # type: ignore[assignment]
            ctx.llm.propose_origins = lambda **kwargs: {  # type: ignore[assignment]
                "origins": [{"title": resolved.title, "year": 1995, "why": "x"}],
                "start_year": 1995,
            }
            self.topic.keywords = ["buffer management", "dynamic threshold", "shared buffer"]
            self.assertEqual(engine.find_origins(self.topic), [], "跑题的奠基工作必须被挡掉")

            # 切题的则保留
            ctx.llm.propose_origins = lambda **kwargs: {  # type: ignore[assignment]
                "origins": [
                    {
                        "title": "Dynamic Queue Length Thresholds for Shared-Memory Packet Switches",
                        "year": 1998,
                        "why": "首次提出动态阈值",
                    }
                ],
                "start_year": 1998,
            }
            engine._paper_by_title = lambda title, threshold=None: Paper(  # type: ignore[assignment]
                title="Dynamic queue length thresholds for shared-memory packet switches",
                doi="10.1/dt",
                year=1998,
                abstract="We propose a dynamic threshold scheme for shared buffer switches.",
            )
            kept = engine.find_origins(self.topic)
            self.assertEqual(len(kept), 1)
            self.assertTrue(kept[0].extra["is_origin"])
        finally:
            ctx.store.close()

    def test_recommendation_to_dict_falls_back_to_paper_score(self):
        """不生成卡片的路径只构造 Recommendation(paper=p)，分数不能因此变成 0。"""
        paper = Paper(title="T", year=2020)
        paper.score = 0.73
        paper.score_parts = {"relevance": 0.67}
        rec = Recommendation(paper=paper)  # score 留空
        d = rec.to_dict()
        self.assertAlmostEqual(d["score"], 0.73)
        self.assertAlmostEqual(d["score_parts"]["relevance"], 0.67)


class TestTitleSimilarity(unittest.TestCase):
    def test_gate_rejects_unrelated_fuzzy_match(self):
        original = "Themis: Scheduling-Aware Buffer Management for HBM-Based Hybrid Buffers"
        self.assertGreater(title_similarity(original, original), 0.99)
        self.assertLess(title_similarity(original, "HSM"), 0.5)
        self.assertLess(title_similarity("Occamy: A Preemptive Buffer Management", "Ocean: a buffer management"), 0.85)


class TestHighScoreMemory(unittest.TestCase):
    """高分记忆：阈值自动记忆、幂等、跨运行身份统一、导出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")
        self.topic = Topic(name="交换机BM")
        self.topic.id = self.store.save_topic(self.topic)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _rec(self, paper, score, **kwargs):
        return Recommendation(paper=paper, score=score, score_parts={"relevance": score}, **kwargs)

    def test_remembers_only_above_threshold(self):
        high = Paper(title="High", doi="10.1/high", source="t", sources=["t"])
        low = Paper(title="Low", doi="10.1/low", source="t", sources=["t"])
        self.store.upsert_papers([high, low])
        added = self.store.remember(
            self.topic,
            [self._rec(high, 0.81, summary="S"), self._rec(low, 0.42, summary="S2")],
            min_score=0.6,
        )
        self.assertEqual([a["title"] for a in added], ["High"])
        self.assertEqual(self.store.remembered_count(), 1)

    def test_remember_is_idempotent_and_keeps_best(self):
        paper = Paper(title="P", doi="10.1/p", source="t", sources=["t"])
        self.store.upsert_papers([paper])
        self.store.remember(self.topic, [self._rec(paper, 0.7, summary="第一次", reason="R")], min_score=0.6)
        self.assertEqual(self.store.remember(self.topic, [self._rec(paper, 0.7)], min_score=0.6), [])
        self.assertEqual(self.store.remembered_count(), 1)
        # 分数取历史最高；已有的概要/理由不被空值覆盖
        self.store.remember(self.topic, [self._rec(paper, 0.93, summary="", reason="")], min_score=0.6)
        row = self.store.list_remembered()[0]
        self.assertEqual(row["score"], 0.93)
        self.assertEqual(row["summary"], "第一次")
        self.assertEqual(row["reason"], "R")

    def test_note_and_forget(self):
        paper = Paper(title="P", doi="10.1/p", source="t", sources=["t"])
        self.store.upsert_papers([paper])
        self.store.remember(self.topic, [self._rec(paper, 0.9)], min_score=0.6)
        self.assertTrue(self.store.update_remembered(paper.key, note="必读", tags=["核心"]))
        row = self.store.list_remembered()[0]
        self.assertEqual(row["note"], "必读")
        self.assertEqual(row["tags"], ["核心"])
        self.assertTrue(self.store.forget_remembered(paper.key))
        self.assertEqual(self.store.remembered_count(), 0)
        self.assertFalse(self.store.forget_remembered("不存在"))

    def test_backfill_from_history(self):
        paper = Paper(title="历史高分", doi="10.1/h", source="t", sources=["t"])
        self.store.upsert_papers([paper])
        self.store.save_recommendations(
            self.topic.id, [self._rec(paper, 0.77, summary="S", reason="R")], status="shown"
        )
        self.assertEqual(self.store.remembered_count(), 0)
        self.assertEqual(len(self.store.backfill_remembered(0.6)), 1)
        self.assertEqual(self.store.remembered_count(), 1)

    def test_canonical_key_prevents_cross_run_duplicates(self):
        """真实缺陷回归：Scholar 无 DOI 先入库、Crossref 带 DOI 后入库，不能变成两行。"""
        scholar = Paper(title="Preemptive Buffer Mgmt", source="google_scholar", sources=["google_scholar"])
        self.store.upsert_papers([scholar])
        index = self.store.title_key_index()
        self.assertEqual(index[normalize_title(scholar.title)], scholar.key)

        crossref = Paper(title="Preemptive Buffer Mgmt!", doi="10.1/abc", source="crossref", sources=["crossref"])
        crossref.canonical_key = index[normalize_title(crossref.title)]
        self.store.upsert_papers([crossref])
        self.assertEqual(self.store.count_papers(), 1, "同一篇论文不该有两行")

    def test_dedupe_papers_repairs_history(self):
        """用原始 SQL 伪造"旧版本写下的"重复行，验证修复作业能合并且不留孤儿。

        （不能再用 upsert_papers 造重复：存储层现在有身份兜底，压根不会写出重复行。）
        """
        self.store.upsert_papers(
            [Paper(title="Same Paper", source="google_scholar", sources=["google_scholar"])]
        )
        # 模拟老数据：绕过身份兜底，直接塞第二行（同一篇论文的带 DOI 版本）
        self.store.conn.execute(
            """INSERT INTO papers (key, title, authors, year, venue, venue_detail, doi, arxiv_id,
               url, abstract, item_type, citations, source, sources, extra, first_seen, last_seen,
               title_norm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "doi:legacy0001",
                "Same paper!",
                "[]",
                2025,
                "EuroSys 2025",
                "",
                "10.1/same",
                "",
                "",
                "长摘要" * 20,
                "conferencePaper",
                9,
                "crossref",
                '["crossref"]',
                "{}",
                "2026-09-15T00:00:00",
                "2026-09-15T00:00:00",
                normalize_title("Same paper!"),
            ),
        )
        self.store.conn.commit()
        self.assertEqual(self.store.count_papers(), 2, "伪造的历史脏数据应有两行")

        # 记忆与推荐挂在"旧的标题键"上，修复时必须跟着搬走
        old_key = self.store.conn.execute(
            "SELECT key FROM papers WHERE key LIKE 'title:%'"
        ).fetchone()["key"]
        self.store.remember(
            self.topic,
            [self._rec(Paper(title="Same Paper"), 0.9, summary="S")],
            min_score=0.6,
        )
        self.store.conn.execute(
            "UPDATE remembered SET paper_key = ? WHERE paper_key != ?", (old_key, old_key)
        )
        self.store.conn.commit()

        result = self.store.dedupe_papers()
        self.assertEqual(result["groups_merged"], 1)
        self.assertEqual(result["rows_removed"], 1)
        self.assertEqual(self.store.count_papers(), 1)
        # 保留的是 DOI 键，且拿到了更正式的数据
        survivor = self.store.conn.execute("SELECT key, venue, citations FROM papers").fetchone()
        self.assertEqual(survivor["key"], "doi:legacy0001")
        self.assertEqual(survivor["venue"], "EuroSys 2025")
        self.assertEqual(survivor["citations"], 9)
        # 记忆要跟过来，且不能变成孤儿
        self.assertEqual(len(self.store.list_remembered()), 1)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT paper_key FROM remembered"
            ).fetchone()["paper_key"],
            "doi:legacy0001",
        )
        for table in ("remembered", "recommendations"):
            orphans = self.store.conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} t LEFT JOIN papers p ON p.key=t.paper_key WHERE p.key IS NULL"
            ).fetchone()["c"]
            self.assertEqual(orphans, 0, f"{table} 不应留下孤儿")
        self.assertEqual(self.store.dedupe_papers()["rows_removed"], 0)

    def test_upsert_never_downgrades_venue_or_doi(self):
        """后续某次只有 arXiv 返回该论文时，不能把正式会议名/出版社 DOI 覆盖掉。"""
        paper = Paper(
            title="Occamy",
            doi="10.1145/3689031.3717495",
            venue="EuroSys 2025",
            source="crossref",
            sources=["crossref"],
        )
        self.store.upsert_papers([paper])
        again = Paper(
            title="Occamy",
            doi="10.48550/arXiv.2501.13570",
            venue="arXiv preprint (cs.NI)",
            arxiv_id="2501.13570",
            source="arxiv",
            sources=["arxiv"],
        )
        self.store.upsert_papers([again])
        row = self.store.conn.execute(
            "SELECT venue, doi, arxiv_id FROM papers WHERE key = ?", (paper.key,)
        ).fetchone()
        self.assertEqual(row["venue"], "EuroSys 2025", "正式会议名只升不降")
        self.assertEqual(row["doi"], "10.1145/3689031.3717495", "出版社 DOI 优先于 arXiv DOI")
        self.assertEqual(row["arxiv_id"], "2501.13570", "其他字段照常补齐")

    def test_doi_quality(self):
        from paper_radar.models import doi_quality

        self.assertEqual(doi_quality(""), 0)
        self.assertEqual(doi_quality("10.48550/arXiv.2501.13570"), 1)
        self.assertEqual(doi_quality("10.1145/3689031.3717495"), 2)
        # 合并时保留更正式的那个 DOI
        pre = Paper(title="X", doi="10.48550/arXiv.2501.1", venue="arXiv preprint", source="a", sources=["a"])
        conf = Paper(title="X", doi="10.1145/1.2", venue="SIGCOMM", source="b", sources=["b"])
        pre.merge(conf)
        self.assertEqual(pre.doi, "10.1145/1.2")
        self.assertEqual(pre.venue, "SIGCOMM")

    def test_bibtex_promotes_formally_published_preprints(self):
        """来自 arXiv 但已正式发表的记录，不该导出成 @misc。"""
        preprint_only = {
            "title": "Only On arXiv",
            "item_type": "preprint",
            "venue": "arXiv preprint (cs.NI)",
            "authors": ["A B"],
            "year": 2026,
        }
        published = {
            "title": "Occamy",
            "item_type": "preprint",
            "venue": "Proceedings of the Twentieth European Conference on Computer Systems",
            "authors": ["A B"],
            "year": 2025,
        }
        bib = render.to_bibtex([preprint_only, published])
        self.assertIn("@misc{", bib, "纯预印本仍应是 @misc")
        self.assertIn("@inproceedings{", bib, "已正式发表的应按会议论文导出")
        self.assertIn("booktitle = {Proceedings of the Twentieth European Conference", bib)

    def test_dedupe_papers_keeps_the_formal_venue(self):
        """合并重复行时，「发表在哪里」要保留正式会议名，而不是 arXiv 占位名。"""
        pre = Paper(
            title="Occamy",
            venue="arXiv (Cornell University)",
            source="openalex",
            sources=["openalex"],
            abstract="短",
        )
        self.store.upsert_papers([pre])
        self.store.conn.execute(
            """INSERT INTO papers (key, title, authors, year, venue, venue_detail, doi, arxiv_id,
               url, abstract, item_type, citations, source, sources, extra, first_seen, last_seen,
               title_norm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "doi:legacy0002",
                "Occamy",
                "[]",
                2025,
                "EuroSys 2025",
                "",
                "10.1/occamy",
                "",
                "",
                "",
                "conferencePaper",
                7,
                "crossref",
                '["crossref"]',
                "{}",
                "2026-09-15T00:00:00",
                "2026-09-15T00:00:00",
                normalize_title("Occamy"),
            ),
        )
        self.store.conn.commit()
        self.store.dedupe_papers()
        self.assertEqual(self.store.count_papers(), 1)
        row = self.store.conn.execute("SELECT key, venue, citations FROM papers").fetchone()
        self.assertIn("EuroSys", row["venue"], "正式会议名应胜出")
        self.assertEqual(row["citations"], 7)

    def test_exporters_cover_all_fields(self):
        item = {
            "key": "k1",
            "title": "Preemptive Buffer Management",
            "authors": ["Hao Li", "Danfeng Shan"],
            "year": 2025,
            "venue": "EuroSys",
            "doi": "10.1145/1.2",
            "url": "https://x.org",
            "score": 0.87,
            "topic_name": "交换机BM",
            "first_seen": "2026-09-16T15:00:00",
            "summary": "概要",
            "highlights": ["特色一"],
            "reason": "理由",
            "note": "必读",
            "item_type": "conferencePaper",
        }
        md = render.export_items([item], "markdown")[0]
        self.assertIn("内容概要", md)
        self.assertIn("交换机BM", md)
        self.assertIn("必读", md)

        bib = render.export_items([item], "bibtex")[0]
        self.assertIn("@inproceedings{Li2025", bib)
        self.assertIn("author = {Hao Li and Danfeng Shan}", bib)
        self.assertIn("booktitle = {EuroSys}", bib)
        self.assertIn("必读", bib)
        self.assertEqual(bib.count("note ="), 1, "note 字段不能重复出现")

        csv_text = render.export_items([item], "csv")[0]
        self.assertIn("Hao Li; Danfeng Shan", csv_text)
        self.assertIn("交换机BM", csv_text)

        payload = json.loads(render.export_items([item], "json")[0])
        self.assertEqual(payload[0]["title"], item["title"])

        with self.assertRaises(ValueError):
            render.export_items([item], "xlsx")

    def test_digest_marks_remembered_items(self):
        item = {"title": "T", "year": 2026, "venue": "V", "score": 0.9, "remembered": True}
        html = render.render_digest(topic_name="X", direction="d", items=[item], generated_at="now")
        self.assertIn("★ 高分记忆", html)
        self.assertIn("★ 高分记忆 1 篇", html)
        self.assertIn("★高分记忆", render.render_text([item], topic_name="X"))


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
