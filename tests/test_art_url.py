"""_art_url triple returns + NowPlaying.parse plumbing."""
import types
import unittest

import lms_cover_display as mod

CFG = types.SimpleNamespace(base_url="http://lms:9000", cover_px=1200)
HI = "ab67616d000082c1"
LO = "ab67616d0000b273"
HASH = "aedf255e5820bb7dbc1be373"


class TestArtUrl(unittest.TestCase):
    def test_scdn_hires_emission(self):
        k, u, h = mod._art_url(CFG, {"artwork_url":
            f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{HI}{HASH}/image.png"})
        self.assertIn(LO + HASH, u)
        self.assertNotIn(HI, u)
        self.assertIn(HI + HASH, h)
        self.assertTrue(h.endswith("_o.png"))
        self.assertIn(LO + HASH, k)     # key normalized to the fast class

    def test_scdn_fast_emission_derives_hires(self):
        k, u, h = mod._art_url(CFG, {"artwork_url":
            f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{LO}{HASH}/image.png"})
        self.assertIn(LO + HASH, u)
        self.assertIn(HI + HASH, h)

    def test_same_key_across_size_classes(self):
        k1, _, _ = mod._art_url(CFG, {"artwork_url":
            f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{HI}{HASH}/image.png"})
        k2, _, _ = mod._art_url(CFG, {"artwork_url":
            f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{LO}{HASH}/image.png"})
        self.assertEqual(k1, k2)

    def test_absolute_external_wrapped(self):
        _, u, h = mod._art_url(CFG, {"artwork_url":
            f"https://i.scdn.co/image/{HI}{HASH}"})
        self.assertIn("/imageproxy/", u)
        self.assertIn(LO, u)
        self.assertIn(HI, h)

    def test_non_scdn_imageproxy_no_hires(self):
        _, u, h = mod._art_url(CFG, {"artwork_url":
            "/imageproxy/https%3A%2F%2Fradio.example%2Flogo.png/image.png"})
        self.assertEqual(h, "")
        self.assertTrue(u.endswith("_o.png"))

    def test_coverid(self):
        k, u, h = mod._art_url(CFG, {"coverid": "42"})
        self.assertEqual(k, "cid:42")
        self.assertEqual(h, "")
        self.assertIn("/music/42/cover_1200x1200_o.png", u)

    def test_coverid_fetches_by_track_id(self):
        # coverid is a path/mtime hash that goes stale when the file is
        # touched after the scan (the /music/<coverid>/ form then 404s
        # forever); the track-id form keeps resolving. Key stays coverid —
        # it is shared across an album's tracks.
        k, u, h = mod._art_url(CFG, {"coverid": "fceef5dd", "id": 26077})
        self.assertEqual(k, "cid:fceef5dd")
        self.assertEqual(h, "")
        self.assertIn("/music/26077/cover_1200x1200_o.png", u)

    def test_coverid_ignores_synthetic_negative_id(self):
        k, u, _ = mod._art_url(CFG, {"coverid": "abc", "id": -140})
        self.assertEqual(k, "cid:abc")
        self.assertIn("/music/abc/cover_1200x1200_o.png", u)

    def test_coverid_accepts_numeric_string_id(self):
        # Perl JSON can emit the id as "26077" — same int/string flip-flop
        # as the remote flag.
        _, u, _ = mod._art_url(CFG, {"coverid": "abc", "id": "26077"})
        self.assertIn("/music/26077/cover_1200x1200_o.png", u)

    def test_remote_library_row_fetches_by_coverid(self):
        # Online-library rows (Spot On imports) have no file: LMS 500s the
        # /music/<track id>/ form for them, while /music/<coverid>/ works.
        k, u, h = mod._art_url(CFG, {"coverid": "e61ff6eb", "id": 137045,
                                     "remote": "1"})
        self.assertEqual(k, "cid:e61ff6eb")
        self.assertEqual(h, "")
        self.assertIn("/music/e61ff6eb/cover_1200x1200_o.png", u)

    def test_placeholder_passthrough(self):
        _, u, h = mod._art_url(CFG, {"artwork_url": "/html/images/cover.png"})
        self.assertEqual(h, "")
        self.assertTrue(u.endswith("/html/images/cover.png"))

    def test_no_art(self):
        self.assertEqual(mod._art_url(CFG, {}), ("", "", ""))


class TestParse(unittest.TestCase):
    def test_current_and_next_fields(self):
        st = {"mode": "play", "playerid": "aa", "playlist_loop": [
            {"title": "c", "artwork_url":
             f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{HI}{HASH}/image.png"},
            {"title": "n", "artwork_url":
             f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{HI}deadbeef/image.png"}]}
        np = mod.NowPlaying.parse(CFG, st)
        self.assertIn(LO, np.cover_url)
        self.assertIn(HI, np.cover_hires_url)
        self.assertIn(LO, np.next_cover_url)
        self.assertIn(HI, np.next_cover_hires_url)

    def test_current_fallback_no_hires(self):
        np = mod.NowPlaying.parse(CFG, {"mode": "play", "playerid": "aa",
                                        "playlist_loop": [{"title": "x"}]})
        self.assertEqual(np.cover_key, "current")
        self.assertEqual(np.cover_hires_url, "")

    def test_remote_flag(self):
        st = {"mode": "play", "remote": 1, "playerid": "aa",
              "playlist_loop": [{"title": "t"}]}
        self.assertTrue(mod.NowPlaying.parse(CFG, st).remote)
        st.pop("remote")
        self.assertFalse(mod.NowPlaying.parse(CFG, st).remote)

    def test_remote_flag_string_zero_is_local(self):
        # Live LMS playlist_loop entries carry remote as the STRING "0" for
        # local tracks; bool("0") is True, which once flipped every local
        # track to remote (radio cover search fired on library songs).
        st = {"mode": "play", "playerid": "aa",
              "playlist_loop": [{"title": "t", "remote": "0",
                                 "coverid": "42", "id": 7}]}
        self.assertFalse(mod.NowPlaying.parse(CFG, st).remote)
        st["playlist_loop"][0]["remote"] = "1"
        self.assertTrue(mod.NowPlaying.parse(CFG, st).remote)

    def test_station_from_remote_title(self):
        st = {"mode": "play", "remote": 1, "playerid": "aa",
              "playlist_loop": [{"title": "Programme",
                                 "remote_title": "Radio Helsinki"}]}
        self.assertEqual(mod.NowPlaying.parse(CFG, st).station,
                         "Radio Helsinki")

    def test_station_cleared_when_it_is_the_title(self):
        # No title tag at all: remote_title becomes the title; don't repeat it.
        st = {"mode": "play", "remote": 1, "playerid": "aa",
              "playlist_loop": [{"remote_title": "Radio Helsinki"}]}
        np = mod.NowPlaying.parse(CFG, st)
        self.assertEqual(np.title, "Radio Helsinki")
        self.assertEqual(np.station, "")

    def test_no_station_for_local_tracks(self):
        st = {"mode": "play", "playerid": "aa",
              "playlist_loop": [{"title": "t", "remote_title": "x"}]}
        self.assertEqual(mod.NowPlaying.parse(CFG, st).station, "")


class TestLibraryArtworkFill(unittest.TestCase):
    """status drops artwork_url (K) for online-library rows; LMSClient
    patches it back in from songinfo so the hi-res path applies."""
    ART = f"/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2F{LO}{HASH}/image.png"

    def make_client(self, songinfo=None, fail=False):
        cfg = types.SimpleNamespace(base_url="http://lms:9000",
                                    request_timeout=1.0)
        client = mod.LMSClient(cfg)
        calls = []

        def request(player, cmd):
            calls.append(cmd)
            if cmd[0] == "status":
                return {"mode": "play", "playlist_loop": [
                    {"id": 137045, "coverid": "e61ff6eb", "remote": "1"},
                    {"id": 137046, "coverid": "e61ff6eb", "remote": "1"}]}
            if fail:
                raise OSError("boom")
            return songinfo if songinfo is not None else {"songinfo_loop": [
                {"id": int(cmd[3].split(":")[1])},
                {"artwork_url": self.ART}]}

        client.request = request
        return client, calls

    def test_fills_current_and_next_then_hires(self):
        client, _ = self.make_client()
        st = client.status("aa")
        for entry in st["playlist_loop"]:
            self.assertEqual(entry["artwork_url"], self.ART)
        np = mod.NowPlaying.parse(CFG, st)
        self.assertIn(HI + HASH, np.cover_hires_url)
        self.assertIn(HI + HASH, np.next_cover_hires_url)

    def test_lookup_cached_per_track(self):
        client, calls = self.make_client()
        client.status("aa")
        client.status("aa")
        self.assertEqual(sum(c[0] == "songinfo" for c in calls), 2)

    def test_lookup_failure_falls_back_to_coverid(self):
        client, calls = self.make_client(fail=True)
        st = client.status("aa")
        np = mod.NowPlaying.parse(CFG, st)
        self.assertEqual(np.cover_key, "cid:e61ff6eb")
        self.assertIn("/music/e61ff6eb/", np.cover_url)
        client.status("aa")   # failures aren't cached: retried next sweep
        self.assertEqual(sum(c[0] == "songinfo" for c in calls), 4)

    def test_no_artwork_cached_as_empty(self):
        client, calls = self.make_client(songinfo={"songinfo_loop": [{"id": 1}]})
        client.status("aa")
        client.status("aa")
        self.assertEqual(sum(c[0] == "songinfo" for c in calls), 2)

    def test_skips_local_streams_and_entries_with_art(self):
        client, calls = self.make_client()
        loop = [{"id": 5, "coverid": "x", "remote": "0"},
                {"id": -140, "coverid": "y", "remote": 1},
                {"id": 6, "remote": "1", "artwork_url": "/keep"}]
        client._fill_library_artwork(loop)
        self.assertEqual(calls, [])
        self.assertNotIn("artwork_url", loop[0])
        self.assertEqual(loop[2]["artwork_url"], "/keep")

    def test_cache_bounded(self):
        client, _ = self.make_client()
        client.LIB_ART_CAP = 3
        for tid in range(1, 6):
            client._fill_library_artwork([{"id": tid, "remote": 1}])
        self.assertEqual(list(client._lib_art), [3, 4, 5])


class TestRadioIdent(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(
            radio_cover_search=True, radio_cover_title_fallback=False)

    def test_radio_with_album(self):
        np = mod.NowPlaying(artist="A", album="B", title="T", remote=True,
                            cover_key="url:/x")
        self.assertEqual(mod._radio_ident(self.cfg, np), ("a", "b"))

    def test_strict_no_album(self):
        np = mod.NowPlaying(artist="A", album="", title="T", remote=True)
        self.assertIsNone(mod._radio_ident(self.cfg, np))

    def test_title_fallback(self):
        self.cfg.radio_cover_title_fallback = True
        np = mod.NowPlaying(artist="A", album="", title="T", remote=True)
        self.assertEqual(mod._radio_ident(self.cfg, np), ("a", "t"))

    def test_spotify_excluded_by_hires(self):
        np = mod.NowPlaying(artist="A", album="B", remote=True,
                            cover_hires_url="http://x")
        self.assertIsNone(mod._radio_ident(self.cfg, np))

    def test_scdn_key_belt(self):
        np = mod.NowPlaying(artist="A", album="B", remote=True,
            cover_key="url:/imageproxy/https%3A%2F%2Fi.scdn.co%2Fimage%2Fabc/image.png")
        self.assertIsNone(mod._radio_ident(self.cfg, np))

    def test_local_track_excluded(self):
        np = mod.NowPlaying(artist="A", album="B", remote=False)
        self.assertIsNone(mod._radio_ident(self.cfg, np))

    def test_master_switch(self):
        self.cfg.radio_cover_search = False
        np = mod.NowPlaying(artist="A", album="B", remote=True)
        self.assertIsNone(mod._radio_ident(self.cfg, np))


if __name__ == "__main__":
    unittest.main()
