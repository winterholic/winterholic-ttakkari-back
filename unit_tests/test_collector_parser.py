"""CP-S2-04/06: pure parsing, normalization, language, duplicate and rule-signal tests. No network, no database."""
from __future__ import annotations

import gzip
import hashlib
import unittest
import zlib
from datetime import UTC, datetime

from ttakkari.collector import parse, quality, textproc
from ttakkari.collector.contracts import CollectorContractError
from ttakkari.collector.fetcher import FetchError, _BoundedDecoder
from ttakkari.collector.profiles import (
    PROFILES,
    ProfileInput,
    get_profile,
    resolve_params,
)

KO = "오늘 서울의 하늘은 맑았고 바람은 약하게 불었다. 시민들은 한강 공원에서 산책을 즐겼으며 자전거를 타는 사람도 많았다."
EN = "The committee published its quarterly report on infrastructure spending this morning and noted a clear increase."


def html(body: str, head: str = "") -> bytes:
    return f"<html><head><title>t</title>{head}</head><body>{body}</body></html>".encode()


class DetectKindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse.detect_kind(html("<p>x</p>"), "text/html").kind, "html")
        self.assertEqual(parse.detect_kind(b"<?xml version='1.0'?><rss version='2.0'></rss>", "text/xml").kind, "feed")
        self.assertEqual(parse.detect_kind(b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>", "application/atom+xml").kind, "feed")
        self.assertEqual(parse.detect_kind(b"<urlset xmlns='x'></urlset>", "application/xml").kind, "sitemap")
        self.assertEqual(parse.detect_kind(b"<sitemapindex xmlns='x'></sitemapindex>", "text/xml").kind, "sitemap")
        self.assertEqual(parse.detect_kind(b"plain words here", "text/plain").kind, "text")
        self.assertEqual(parse.detect_kind(html("<p>x</p>"), "application/octet-stream").kind, "html")

    def test_mismatch_and_binary_are_unsupported_with_a_reason(self):
        cases = [
            (b"%PDF-1.4 ...", "application/pdf", "pdf_not_supported"),
            (b"%PDF-1.4 ...", "text/html", "pdf_not_supported"),
            (b"\x89PNG\r\n\x1a\n", "image/png", "binary_content"),
            (b"PK\x03\x04zip", "application/zip", "binary_content"),
            (b"a\x00b" * 10, "text/html", "binary_content"),
            (html("<p>x</p>"), "text/plain", "media_type_mismatch"),
            (b"{}", "application/json", "media_type_unsupported"),
        ]
        for raw, declared, reason in cases:
            with self.subTest(declared=declared, reason=reason):
                got = parse.detect_kind(raw, declared)
                self.assertEqual((got.kind, got.reason), ("unsupported", reason))


class ExtractTests(unittest.TestCase):
    def test_main_text_without_navigation_and_with_metadata(self):
        page = html(
            f"<nav>menu menu menu</nav><article><h1>제목</h1><p>{KO}</p></article><footer>footer links</footer>",
            '<meta property="article:published_time" content="2026-10-01T09:00:00+09:00"/>'
            '<link rel="canonical" href="https://example.test/canonical"/><meta name="generator" content="Docs 4.2"/>',
        )
        doc = parse.extract_document(page, url="https://example.test/a", kind="html")
        self.assertIn("한강 공원", doc.text)
        self.assertNotIn("menu menu", doc.text)
        self.assertEqual((doc.published.date().isoformat(), doc.published_precision), ("2026-10-01", "date"))
        self.assertEqual(doc.canonical_url, "https://example.test/canonical")
        self.assertEqual(doc.meta["generator"], "Docs 4.2")

    def test_empty_extraction_is_an_error_not_an_empty_document(self):
        with self.assertRaises(ValueError) as ctx:
            parse.extract_document(html("<script>var x = 1;</script>"), url="https://example.test/", kind="html")
        self.assertEqual(str(ctx.exception), "empty_extraction")

    def test_declared_and_detected_charsets(self):
        euc = KO.encode("euc-kr")
        text, enc = parse.decode_bytes(euc, "euc-kr")
        self.assertEqual((text, enc), (KO, "euc-kr"))
        self.assertEqual(parse.decode_bytes(euc, None)[0].count("한강"), 1)  # detected without a hint
        self.assertEqual(parse.decode_bytes(KO.encode("utf-8"), "no-such-charset")[0], KO)
        self.assertEqual(parse.decode_bytes(b"\xef\xbb\xbf" + b"bom", "utf-8")[0], "bom")

    def test_text_cap_marks_truncation(self):
        long = "word " * 300_000
        doc = parse.extract_document(long.encode(), url="https://example.test/t", kind="text")
        self.assertTrue(doc.truncated and len(doc.text) == parse.MAX_TEXT_CHARS)

    def test_version_hints(self):
        f = parse.find_version_hint
        self.assertEqual(f("https://d.test/3.13/library/os.html", "", {}), ("3.13", "url"))
        self.assertEqual(f("https://d.test/v2/guide", "", {}), ("2", "url"))
        self.assertEqual(f("https://d.test/guide", "Version: 4.1.2\nbody", {}), ("4.1.2", "text"))
        self.assertEqual(f("https://d.test/guide", "x", {"doc_version": "5.0"}), ("5.0", "meta"))
        self.assertEqual(f("https://d.test/page/2024", "plain", {}), (None, None))  # a year is not a version
        self.assertTrue(parse.is_latest_alias("https://d.test/en/latest/api") and not parse.is_latest_alias("https://d.test/3.1/api"))


class DiscoveryParseTests(unittest.TestCase):
    def test_rss_atom_sitemap_and_links(self):
        rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><link>/a</link></item><item><link>https://x.test/b#frag</link></item><item><link>/a</link></item></channel></rss>"""
        self.assertEqual(parse.parse_feed(rss, "https://x.test/feed"), ["https://x.test/a", "https://x.test/b"])
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="/e1"/></entry><entry><link rel="self" href="/no"/><link href="/e2"/></entry></feed>"""
        self.assertEqual(parse.parse_feed(atom, "https://x.test/atom"), ["https://x.test/e1", "https://x.test/e2"])
        index = b"""<sitemapindex xmlns="s"><sitemap><loc>https://x.test/s2.xml</loc></sitemap></sitemapindex>"""
        self.assertEqual(parse.parse_sitemap(index, "https://x.test/"), ([], ["https://x.test/s2.xml"]))
        urlset = b"""<urlset xmlns="s"><url><loc>https://x.test/p</loc></url></urlset>"""
        self.assertEqual(parse.parse_sitemap(urlset, "https://x.test/"), (["https://x.test/p"], []))
        links = parse.extract_links(html("<a href='/ok'>a</a><a href='mailto:a@b'>m</a><a href='/f.pdf'>p</a><a href='#top'>t</a><a href='/ok'>dup</a>"), "https://x.test/")
        self.assertEqual(links, ["https://x.test/ok"])

    def test_xml_entities_are_not_expanded_or_fetched(self):
        bomb = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY x SYSTEM "file:///etc/passwd">]>
            <rss><channel><item><link>/&b;&x;</link></item></channel></rss>"""
        urls = parse.parse_feed(bomb, "https://x.test/")
        self.assertTrue(all("root:" not in u and "aaaaaaaa" not in u for u in urls))


class NormalizeTests(unittest.TestCase):
    def test_nfc_not_nfkc_and_every_change_is_counted(self):
        out = textproc.normalize_text("Café ＡＢＣ  x\u200b\r\n\r\n\r\n\r\ny\x00 z\t\tend  ")
        self.assertEqual(out.text, "Café ＡＢＣ x\n\ny z end")  # full-width letters survive: NFC only
        ops = dict(out.steps)
        for op in ("nfc", "line_endings", "strip_zero_width", "strip_control", "collapse_spaces", "collapse_blank_lines"):
            self.assertIn(op, ops)

    def test_clean_text_has_no_steps_and_truncation_is_flagged(self):
        clean = textproc.normalize_text("already clean\n\nparagraph two")
        self.assertEqual(clean.steps, ())
        cut = textproc.normalize_text("x" * 50, max_chars=10)
        self.assertTrue(cut.truncated and cut.text == "x" * 10 and cut.steps[-1][0] == "truncate")

    def test_normalization_is_idempotent(self):
        once = textproc.normalize_text("A\u200bB \r\n\r\n\r\nC\x01")
        self.assertEqual(textproc.normalize_text(once.text).text, once.text)


class LanguageTests(unittest.TestCase):
    def test_script_and_model_paths(self):
        cases = [(KO, "ko"), (EN, "en"), ("Der schnelle braune Fuchs springt über den faulen Hund am Flussufer heute.", "de"),
                 ("今日はとても良い天気ですね。散歩に行きましょう。公園で桜を見ましょう。", "ja")]
        for text, lang in cases:
            with self.subTest(lang=lang):
                self.assertEqual(textproc.identify_language(text).language, lang)

    def test_too_short_is_und_with_zero_confidence_not_a_guess(self):
        got = textproc.identify_language("short")
        self.assertEqual((got.language, got.confidence, got.method), ("und", 0.0, "too_short"))


class DedupeTests(unittest.TestCase):
    base = " ".join(f"문장 {i} 은 서로 다른 내용을 담고 있으며 번호 {i * 7} 와 {i * 13} 을 포함한다." for i in range(30))

    def test_signature_properties(self):
        sig = textproc.minhash_signature(self.base)
        self.assertEqual(len(sig), 128 * 8)
        self.assertEqual(sig, textproc.minhash_signature(self.base))  # deterministic across calls
        self.assertIsNone(textproc.minhash_signature("too short"))
        self.assertEqual(len(textproc.band_hashes(sig)), 16)

    def test_similarity_orders_identical_near_and_unrelated(self):
        sig = textproc.minhash_signature(self.base)
        near = textproc.minhash_signature(self.base.replace("문장 3 ", "문장 삼 ").replace("번호 70", "번호 71"))
        other = textproc.minhash_signature(" ".join(f"completely different english sentence number {i} about rivers {i * 3}" for i in range(40)))
        self.assertEqual(textproc.estimated_jaccard(sig, sig), 1.0)
        self.assertGreaterEqual(textproc.estimated_jaccard(sig, near), textproc.NEAR_THRESHOLD)
        self.assertLess(textproc.estimated_jaccard(sig, other), 0.1)
        shared = set(textproc.band_hashes(sig)) & set(textproc.band_hashes(near))
        self.assertTrue(shared, "a near duplicate must share at least one LSH band")
        self.assertFalse(set(textproc.band_hashes(sig)) & set(textproc.band_hashes(other)))

    def test_dedupe_key_ignores_case_punctuation_width_but_not_content(self):
        self.assertEqual(textproc.dedupe_key_text("Hello,  World!"), textproc.dedupe_key_text("ｈｅｌｌｏ world"))
        self.assertNotEqual(textproc.dedupe_key_text("Hello World"), textproc.dedupe_key_text("Hello Worlds"))

    def test_exact_hash_is_over_the_normalized_text(self):
        t = "정확 중복 비교용 본문"
        self.assertEqual(textproc.sha256_text(t), hashlib.sha256(t.encode()).hexdigest())


class L0Tests(unittest.TestCase):
    def flags(self, text: str, thresholds=None) -> set[str]:
        return set(quality.l0_signals(text, thresholds).flags)

    def test_clean_prose_has_no_flags(self):
        text = "\n".join(f"Paragraph {i} discusses a different topic with its own details, numbers {i * 11} and conclusions {i * 3}." for i in range(8))
        self.assertEqual(self.flags(text, quality.L0Thresholds(min_chars=100)), set())

    def test_each_rule_fires_alone_and_flags_are_only_holds(self):
        self.assertEqual(self.flags("tiny"), {"l0_too_short"})
        self.assertIn("l0_repetitive", self.flags("buy now cheap pills " * 80))
        self.assertIn("l0_repeated_lines", self.flags("\n".join(["same line again and again here"] * 10 + ["unique one", "unique two"])))
        self.assertIn("l0_symbol_heavy", self.flags("#@!$%^&*() " * 60))
        self.assertIn("l0_link_heavy", self.flags(" ".join(f"http://x.test/{i}" for i in range(60))))
        self.assertIn("l0_mostly_short_lines", self.flags("\n".join(f"item {i}" for i in range(40)) + " " * 200 + "tail", quality.L0Thresholds(min_chars=10)))

    def test_thresholds_are_per_profile(self):
        code = "\n".join(f"x = foo({i}); y = bar[{i}] + {i * 2};" for i in range(40))
        self.assertIn("l0_symbol_heavy", self.flags(code))
        tech = get_profile("tech_docs").l0_thresholds(get_profile("tech_docs").defaults)
        self.assertNotIn("l0_symbol_heavy", self.flags(code, tech))

    def test_empty_text_does_not_divide_by_zero(self):
        self.assertEqual(quality.l0_signals("").signals["char_count"], 0)


class L1PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_validation(self):
        for kwargs in ({"mode": "x"}, {"mode": "gate"}, {"mode": "gate", "scorer": "s", "hold_below": {"nope": 1}},
                       {"mode": "shadow", "scorer": "s", "hold_below": {"spam": 1}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception) as ctx:
                quality.L1Policy(**kwargs)
            self.assertEqual(type(ctx.exception).__name__, "CollectorContractError")

    async def test_off_and_unavailable_never_report_a_zero_score(self):
        for policy, err in ((quality.L1Policy(), None), (quality.L1Policy("gate", "missing"), "scorer_unavailable")):
            out = await quality.run_l1(policy, "text", "en", "pre-1")
            self.assertEqual(out.error, err)
            self.assertTrue(all(a["status"] == "unmeasured" and a["value"] is None for a in out.quality.values()))

    async def test_scorer_errors_and_bad_output_become_errors_not_scores(self):
        class Boom:
            info = quality.ScorerInfo("boom", "m", "r", "p")

            async def score(self, text, language):
                raise RuntimeError("model crashed")

        class BadScale:
            info = quality.ScorerInfo("bad", "m", "r", "p")

            async def score(self, text, language):
                return {"education": quality.AxisScore(1.0, "percent")}

        for scorer, err in ((Boom(), "scorer_error:RuntimeError"), (BadScale(), "scorer_bad_scale:education")):
            quality.register_scorer(scorer)
            try:
                out = await quality.run_l1(quality.L1Policy("gate", scorer.info.name), "text", "en", "pre-1")
            finally:
                quality.unregister_scorer(scorer.info.name)
            self.assertEqual(out.error, err)
            self.assertEqual(out.quality["education"]["status"], "unmeasured")


class ProfileTests(unittest.TestCase):
    def doc(self, **kw) -> ProfileInput:
        d = {"url": "https://x.test/a", "text": "body", "title": "t", "published": None, "published_precision": None,
             "last_modified": None, "fetched_at": datetime(2026, 10, 21, tzinfo=UTC), "meta": {}}
        d.update(kw)
        return ProfileInput(**d)

    def test_registry_and_param_validation(self):
        self.assertEqual(set(PROFILES), {"general", "news", "tech_docs"})
        news = get_profile("news")
        self.assertEqual(resolve_params(news, {"max_age_days": 30})["max_age_days"], 30)
        for bad in ({"max_age_days": 0}, {"max_age_days": 99999}, {"max_age_days": 1.5}, {"max_age_days": True}, {"x": 1}, {"require_published_at": 1}):
            with self.subTest(bad=bad), self.assertRaises(CollectorContractError):
                resolve_params(news, bad)
        with self.assertRaises(CollectorContractError):
            get_profile("unknown")

    def test_news_age_rules(self):
        news, params = get_profile("news"), get_profile("news").defaults
        fresh = news.evaluate(self.doc(published=datetime(2026, 10, 20, tzinfo=UTC)), params)
        self.assertEqual((fresh.hold_reasons, fresh.signals["age_days"]), ((), 1.0))
        self.assertEqual(news.evaluate(self.doc(published=datetime(2026, 9, 1, tzinfo=UTC)), params).hold_reasons, ("news_stale",))
        self.assertEqual(news.evaluate(self.doc(published=datetime(2026, 12, 1, tzinfo=UTC)), params).hold_reasons, ("news_published_in_future",))
        self.assertEqual(news.evaluate(self.doc(), params).hold_reasons, ("news_published_at_unknown",))
        self.assertEqual(news.evaluate(self.doc(), {**params, "require_published_at": False}).hold_reasons, ())
        # Last-Modified is a signal only; it never stands in for a publication date
        lm = news.evaluate(self.doc(last_modified=datetime(2026, 10, 20, tzinfo=UTC)), params)
        self.assertEqual(lm.hold_reasons, ("news_published_at_unknown",))
        self.assertIsNotNone(lm.signals["last_modified_header"])


class BoundedDecoderTests(unittest.TestCase):
    def feed_all(self, encoding: str, payload: bytes, limit: int, chunk: int = 1000) -> bytes:
        dec = _BoundedDecoder(encoding, limit)
        out = b"".join(dec.feed(payload[i : i + chunk]) for i in range(0, len(payload), chunk))
        dec.finish()
        return out

    def test_roundtrip_and_cap(self):
        data = b"hello world " * 1000
        self.assertEqual(self.feed_all("gzip", gzip.compress(data), 100_000), data)
        self.assertEqual(self.feed_all("deflate", zlib.compress(data), 100_000), data)
        co = zlib.compressobj(wbits=-zlib.MAX_WBITS)  # raw deflate, as some servers send it
        self.assertEqual(self.feed_all("deflate", co.compress(data) + co.flush(), 100_000), data)
        self.assertEqual(self.feed_all("identity", data, 100_000), data)
        for enc, payload in (("gzip", gzip.compress(b"\0" * 20_000_000)), ("deflate", zlib.compress(b"\0" * 20_000_000))):
            with self.subTest(enc=enc), self.assertRaises(FetchError) as ctx:
                self.feed_all(enc, payload, 100_000)
            self.assertEqual(ctx.exception.code, "response_too_large")

    def test_corrupt_and_truncated_streams(self):
        with self.assertRaises(FetchError) as corrupt:
            self.feed_all("gzip", b"not gzip at all" * 10, 1000)
        self.assertEqual(corrupt.exception.code, "content_encoding_invalid")
        with self.assertRaises(FetchError) as cut:
            self.feed_all("gzip", gzip.compress(b"x" * 5000)[:25], 100_000)
        self.assertEqual(cut.exception.code, "content_encoding_invalid")


if __name__ == "__main__":
    unittest.main()
