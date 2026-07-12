"""_radio_cover_search JSONL parsing/selection against canned streams
(urlopen monkeypatched — no network)."""
import io
import json
import os
import types
import unittest
from unittest import mock

import lms_cover_display as mod


def cfg(**over):
    base = dict(radio_cover_country="de", radio_cover_timeout=8.0,
                radio_cover_sources=["applemusic", "tidal", "amazonmusic",
                                     "spotify"],
                radio_cover_loose_match=True)
    base.update(over)
    return types.SimpleNamespace(**base)


def stream(events, done_sources=()):
    lines = [json.dumps(e) for e in events]
    lines += [json.dumps({"type": "done", "success": True, "source": s})
              for s in done_sources]
    return ("\n".join(lines) + "\n").encode()


def cover(source, artist, title, url):
    return {"type": "cover", "source": source, "bigCoverUrl": url,
            "releaseInfo": {"artist": artist, "title": title}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def patched(body):
    return mock.patch.object(mod.urllib.request, "urlopen",
                             return_value=FakeResponse(body))


ALL = ("applemusic", "tidal", "amazonmusic", "spotify")


class TestSearchSelection(unittest.TestCase):
    def test_exact_hit_source_order(self):
        body = stream([
            cover("spotify", "A", "X", "http://s/1"),
            cover("applemusic", "A", "X", "http://a/1"),
        ], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[0], "ok")
        self.assertEqual(r[1][0], ("http://a/1", "applemusic", 1))
        self.assertEqual(r[1][1], ("http://s/1", "spotify", 1))

    def test_tier_order_beats_source_order(self):
        body = stream([
            cover("applemusic", "A", "X (Deluxe)", "http://a/3"),
            cover("spotify", "A", "X", "http://s/1"),
        ], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[1][0][2], 1)         # spotify tier 1 first
        self.assertEqual(r[1][0][1], "spotify")
        self.assertEqual(r[1][1][2], 3)

    def test_dedup_by_url(self):
        body = stream([
            cover("applemusic", "A", "X", "http://same/url"),
            cover("tidal", "A", "X", "http://same/url"),
        ], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(len(r[1]), 1)

    def test_cap_five(self):
        events = [cover(s, "A", "X", f"http://{s}/{i}")
                  for s in ALL for i in range(3)]
        body = stream(events, ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertLessEqual(len(r[1]), 5)

    def test_video_urls_skipped(self):
        body = stream([
            cover("applemusic", "A", "X", "https://mvod.itunes.apple.com/x.mp4"),
            cover("applemusic", "A", "X", "http://a/still.mp4"),
            cover("tidal", "A", "X", "http://t/ok.jpg"),
        ], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual([c[0] for c in r[1]], ["http://t/ok.jpg"])

    def test_loose_match_off_collects_tier1_only(self):
        body = stream([
            cover("applemusic", "A", "X (Deluxe)", "http://a/3"),
            cover("tidal", "A", "X", "http://t/1"),
        ], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(radio_cover_loose_match=False),
                                        "A", "X")
        self.assertEqual([c[2] for c in r[1]], [1])

    def test_nomatch_definitive(self):
        body = stream([cover("applemusic", "Other", "Thing", "http://a/1")], ALL)
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r, ("nomatch",))

    def test_partial_eof_top_source_done_is_definitive(self):
        body = stream([], done_sources=("applemusic",))
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r, ("nomatch",))

    def test_partial_eof_top_source_missing_is_transient(self):
        body = stream([], done_sources=("tidal",))
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[0], "error")

    def test_gate_refusal_text_is_transient_and_flagged(self):
        body = (b"Please do not use the internal API directly. "
                b"Consult the integrations section on the website.")
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[0], "error")
        self.assertIn("gate", r[1])

    def test_malformed_lines_skipped(self):
        body = (b'not json at all\n'
                + json.dumps(cover("applemusic", "A", "X", "http://a/1")).encode()
                + b"\n"
                + stream([], ALL))
        with patched(body):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[0], "ok")

    def test_http_error_is_transient(self):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            r = mod._radio_cover_search(cfg(), "A", "X")
        self.assertEqual(r[0], "error")


@unittest.skipUnless(os.environ.get("RUN_LIVE") == "1",
                     "live API tests only with RUN_LIVE=1")
class TestSearchLive(unittest.TestCase):
    def test_known_tier3_queries(self):
        for artist, album in (("Lou Reed", "Rock N Roll Animal"),
                              ("Bush", "Sixteen Stone"),
                              ("Rhye", "JOY - EP")):
            r = mod._radio_cover_search(cfg(), artist, album)
            self.assertEqual(r[0], "ok", (artist, album, r))
            self.assertEqual(r[1][0][2], 3)


if __name__ == "__main__":
    unittest.main()
