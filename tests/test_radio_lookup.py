"""_RadioLookup state machine: arming, learning, gating, stage stepping.
All network/display touch points are monkeypatched — pure logic tests."""
import types
import unittest
from unittest import mock

import lms_cover_display as mod


def cfg(**over):
    base = dict(radio_cover_search=True, radio_cover_title_fallback=False,
                radio_cover_country="de", radio_cover_timeout=8.0,
                radio_cover_sources=["applemusic", "tidal"],
                radio_cover_loose_match=True,
                radio_cover_match_threshold=16,
                upgrade_fade_seconds=0.0,
                base_url="http://lms:9000", cover_px=1200)
    base.update(over)
    return types.SimpleNamespace(**base)


def np_(artist="A", album="B", title="T", key="url:/art1", remote=True):
    return mod.NowPlaying(mode="play", artist=artist, album=album,
                          title=title, remote=remote, cover_key=key)


class FakeArt:
    def __init__(self, surface=None):
        self.surface = surface
        self.cache = {}


class FakeDisplay:
    pygame = None

    def prewarm_background(self, surf):
        pass

    def crossfade(self, surf, np, alpha, seconds):
        self.painted = surf


class TestObserve(unittest.TestCase):
    def setUp(self):
        self.r = mod._RadioLookup()
        self.cfg = cfg()

    def test_new_ident_arms_search(self):
        self.r.observe(self.cfg, np_(), {})
        self.assertEqual(self.r.stage, "search")
        self.assertEqual(self.r.ident, ("a", "b"))

    def test_negative_cached_ident_does_not_arm(self):
        self.r.neg[("a", "b")] = True
        self.r.observe(self.cfg, np_(), {})
        self.assertIsNone(self.r.stage)

    def test_cached_cover_adopted_without_search(self):
        surf = object()
        self.r.observe(self.cfg, np_(), {("radio", "a", "b"): (surf, True)})
        self.assertIs(self.r.surface, surf)
        self.assertIsNone(self.r.stage)

    def test_ident_change_clears_transients_keeps_knowledge(self):
        self.r.observe(self.cfg, np_(), {})
        self.r.try_n, self.r.next_try = 2, 123.0
        self.r.backoff_until = 456.0
        self.r.neg[("x", "y")] = True
        self.r.observe(self.cfg, np_(album="C"), {})
        self.assertEqual(self.r.try_n, 0)
        self.assertEqual(self.r.next_try, 0.0)
        self.assertEqual(self.r.backoff_until, 456.0)   # survives (storm fix)
        self.assertIn(("x", "y"), self.r.neg)

    def test_boundary_sweep_does_not_learn(self):
        # Song boundary: ident changes while the art key still belongs to
        # the previous song — must NOT be learned (B2 sentinel-mislabel fix).
        self.r.observe(self.cfg, np_(album="B", key="url:/old-art"), {})
        self.assertNotIn("url:/old-art", self.r.key_idents)

    def test_stable_sweep_learns_then_sentinels_logo(self):
        self.r.observe(self.cfg, np_(key="url:/logo"), {})       # boundary
        self.r.observe(self.cfg, np_(key="url:/logo"), {})       # stable
        self.assertEqual(self.r.key_idents["url:/logo"], ("a", "b"))
        self.r.observe(self.cfg, np_(album="C", key="url:/logo"), {})  # bnd
        self.r.observe(self.cfg, np_(album="C", key="url:/logo"), {})  # stbl
        self.assertIs(self.r.key_idents["url:/logo"], mod._RadioLookup.LOGO)

    def test_per_song_art_true_for_unique_key(self):
        self.r.observe(self.cfg, np_(key="url:/k1"), {})
        self.r.observe(self.cfg, np_(key="url:/k1"), {})
        self.assertTrue(self.r.per_song_art(np_(key="url:/k1")))
        self.assertFalse(self.r.per_song_art(np_(key="current")))


class TestReadyGuard(unittest.TestCase):
    def setUp(self):
        self.r = mod._RadioLookup()
        self.cfg = cfg()
        self.r.observe(self.cfg, np_(), {})

    def test_ready_when_armed_and_engaged(self):
        self.assertTrue(self.r.ready(100.0, self.cfg, np_(),
                                     mod.S_PLAYING, False))

    def test_not_ready_on_events_queued(self):
        self.assertFalse(self.r.ready(100.0, self.cfg, np_(),
                                      mod.S_PLAYING, True))

    def test_not_ready_when_not_engaged(self):
        self.assertFalse(self.r.ready(100.0, self.cfg, np_(),
                                      mod.S_STOPPED, False))

    def test_not_ready_during_backoff(self):
        self.r.backoff_until = 200.0
        self.assertFalse(self.r.ready(100.0, self.cfg, np_(),
                                      mod.S_PLAYING, False))

    def test_not_ready_when_song_moved_on(self):
        self.assertFalse(self.r.ready(100.0, self.cfg, np_(album="Other"),
                                      mod.S_PLAYING, False))


class TestStepSearch(unittest.TestCase):
    def setUp(self):
        self.r = mod._RadioLookup()
        self.cfg = cfg()
        self.r.observe(self.cfg, np_(), {})
        self.art = FakeArt()

    def step(self, now=100.0):
        return self.r.step(now, self.cfg, np_(), None, FakeDisplay(),
                           self.art, lambda: 255)

    def test_search_ok_advances_to_fetch(self):
        with mock.patch.object(mod, "_radio_cover_search",
                               return_value=("ok", [("http://u", "tidal", 1)])):
            painted, wake = self.step()
        self.assertFalse(painted)
        self.assertEqual(self.r.stage, ("fetch", [("http://u", "tidal", 1)]))
        self.assertEqual(self.r.try_n, 0)

    def test_search_nomatch_negative_caches(self):
        with mock.patch.object(mod, "_radio_cover_search",
                               return_value=("nomatch",)):
            self.step()
        self.assertIsNone(self.r.stage)
        self.assertIn(("a", "b"), self.r.neg)

    def test_search_transient_paces_and_backs_off(self):
        with mock.patch.object(mod, "_radio_cover_search",
                               return_value=("error", "boom")):
            painted, wake = self.step(now=100.0)
        self.assertEqual(self.r.stage, "search")     # still armed
        self.assertEqual(self.r.try_n, 1)
        self.assertEqual(self.r.backoff_until, 120.0)
        self.assertEqual(wake, 120.0)                # max(next_try, backoff)

    def test_search_transient_gives_up_after_budget(self):
        with mock.patch.object(mod, "_radio_cover_search",
                               return_value=("error", "boom")):
            self.step(); self.step(); self.step()
        self.assertIsNone(self.r.stage)
        self.assertNotIn(("a", "b"), self.r.neg)     # NOT negative-cached


class TestStepFetch(unittest.TestCase):
    def setUp(self):
        self.r = mod._RadioLookup()
        self.cfg = cfg()
        self.np = np_(key="url:/k1")
        self.r.observe(self.cfg, self.np, {})
        self.r.observe(self.cfg, self.np, {})        # stable: learn key
        self.art = FakeArt(surface=object())

    def step(self, now=100.0, art=None):
        return self.r.step(now, self.cfg, self.np, None, FakeDisplay(),
                           art or self.art, lambda: 255)

    def test_paint_with_passing_dhash(self):
        self.r.stage = ("fetch", [("http://u", "tidal", 1)])
        surf = object()
        with mock.patch.object(mod, "_fetch_cover", return_value=surf), \
             mock.patch.object(mod, "_dhash", return_value=0), \
             mock.patch.object(mod, "_hamming", return_value=3):
            painted, wake = self.step()
        self.assertTrue(painted)
        self.assertIs(self.r.surface, surf)
        self.assertIn(("radio", "a", "b"), self.art.cache)
        self.assertIsNone(self.r.stage)

    def test_reject_walks_to_next_candidate(self):
        self.r.stage = ("fetch", [("http://u1", "applemusic", 1),
                                  ("http://u2", "tidal", 1)])
        with mock.patch.object(mod, "_fetch_cover", return_value=object()), \
             mock.patch.object(mod, "_dhash", return_value=0), \
             mock.patch.object(mod, "_hamming", return_value=18):
            painted, _ = self.step()
        self.assertFalse(painted)
        self.assertEqual(self.r.stage, ("fetch", [("http://u2", "tidal", 1)]))

    def test_far_miss_aborts_chain(self):
        self.r.stage = ("fetch", [("http://u1", "applemusic", 1),
                                  ("http://u2", "tidal", 1)])
        with mock.patch.object(mod, "_fetch_cover", return_value=object()), \
             mock.patch.object(mod, "_dhash", return_value=0), \
             mock.patch.object(mod, "_hamming", return_value=26):
            self.step()
        self.assertIsNone(self.r.stage)

    def test_no_reference_paints_tier1_without_dhash(self):
        art = FakeArt(surface=None)                   # no station art at all
        self.r.key_idents.clear()                     # and no key knowledge
        self.r.stage = ("fetch", [("http://u", "tidal", 1)])
        with mock.patch.object(mod, "_fetch_cover", return_value=object()):
            painted, _ = self.step(art=art)
        self.assertTrue(painted)

    def test_loose_only_no_reference_negative_caches(self):
        art = FakeArt(surface=None)
        self.r.key_idents.clear()
        self.r.stage = ("fetch", [("http://u", "tidal", 3)])
        painted, _ = self.step(art=art)
        self.assertFalse(painted)
        self.assertIsNone(self.r.stage)
        self.assertIn(("a", "b"), self.r.neg)

    def test_loose_only_art_mid_retry_waits_not_negs(self):
        art = FakeArt(surface=None)                   # art fetch in flight
        self.r.stage = ("fetch", [("http://u", "tidal", 3)])
        painted, wake = self.step(art=art)            # key IS per-song
        self.assertIsNotNone(wake)
        self.assertEqual(self.r.stage, ("fetch", [("http://u", "tidal", 3)]))
        self.assertNotIn(("a", "b"), self.r.neg)

    def test_fetch_failure_retries_then_next_candidate(self):
        self.r.stage = ("fetch", [("http://u1", "applemusic", 1),
                                  ("http://u2", "tidal", 1)])
        with mock.patch.object(mod, "_fetch_cover", return_value=None):
            self.step(); self.step(); painted, _ = self.step()
        self.assertEqual(self.r.stage, ("fetch", [("http://u2", "tidal", 1)]))
        self.assertEqual(self.r.try_n, 0)             # fresh budget


class TestSwitchReset(unittest.TestCase):
    def test_switch_clears_transients_keeps_knowledge(self):
        r = mod._RadioLookup()
        r.observe(cfg(), np_(), {})
        r.neg[("x", "y")] = True
        r.key_idents["k"] = ("x", "y")
        r.backoff_until = 99.0
        r.reset_for_switch()
        self.assertIsNone(r.ident)
        self.assertIsNone(r.surface)
        self.assertIsNone(r.stage)
        self.assertIn(("x", "y"), r.neg)
        self.assertEqual(r.key_idents["k"], ("x", "y"))
        self.assertEqual(r.backoff_until, 99.0)


if __name__ == "__main__":
    unittest.main()
