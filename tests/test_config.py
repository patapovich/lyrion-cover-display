"""load_config validation and clamps (via a temp config file)."""
import os
import tempfile
import unittest

import lms_cover_display as mod


def load(extra=""):
    body = "[lms]\nserver_host = 1.2.3.4\n" + extra
    with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        return mod.load_config(path)
    finally:
        os.unlink(path)


class TestLoadConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load()
        self.assertEqual(cfg.radio_cover_country, "de")
        self.assertTrue(cfg.radio_cover_search)
        self.assertEqual(cfg.radio_cover_sources,
                         ["applemusic", "tidal", "amazonmusic", "spotify"])
        self.assertFalse(cfg.radio_cover_title_fallback)
        self.assertEqual(cfg.radio_cover_match_threshold, 16)
        self.assertAlmostEqual(cfg.upgrade_fade_seconds, 0.4)

    def test_invalid_country_falls_back(self):
        self.assertEqual(load("radio_cover_country = xyz\n")
                         .radio_cover_country, "de")
        self.assertEqual(load("radio_cover_country = f1\n")
                         .radio_cover_country, "de")

    def test_valid_country_kept(self):
        self.assertEqual(load("radio_cover_country = GB\n")
                         .radio_cover_country, "gb")

    def test_empty_sources_disable_search(self):
        cfg = load("radio_cover_sources =\n")
        self.assertFalse(cfg.radio_cover_search)

    def test_threshold_clamped(self):
        self.assertEqual(load("radio_cover_match_threshold = 99\n")
                         .radio_cover_match_threshold, 64)
        self.assertEqual(load("radio_cover_match_threshold = -3\n")
                         .radio_cover_match_threshold, 0)

    def test_fade_clamped(self):
        self.assertAlmostEqual(load("upgrade_fade_seconds = 9\n")
                               .upgrade_fade_seconds, 2.0)
        self.assertAlmostEqual(load("upgrade_fade_seconds = -1\n")
                               .upgrade_fade_seconds, 0.0)

    def test_timeout_floor(self):
        self.assertGreaterEqual(load("radio_cover_timeout = 0.1\n")
                                .radio_cover_timeout, 1.0)


class TestTrimCache(unittest.TestCase):
    def test_fifo(self):
        d = {i: i for i in range(8)}
        mod._trim_cache(d, 5)
        self.assertEqual(list(d), [3, 4, 5, 6, 7])

    def test_restore_keeps_position(self):
        d = {"a": 1, "b": 2, "c": 3}
        d["a"] = 99                       # re-store: keeps insertion slot
        mod._trim_cache(d, 2)
        self.assertEqual(list(d), ["b", "c"])


if __name__ == "__main__":
    unittest.main()
