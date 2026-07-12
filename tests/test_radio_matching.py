"""Tier/normalization/credit matching for the radio cover search."""
import unittest

import lms_cover_display as mod

T = mod._radio_tier
N = mod._radio_norm
C = mod._credit_segments


class TestRadioNorm(unittest.TestCase):
    def test_keeps_hyphen(self):
        self.assertEqual(N("JOY - EP"), "joy - ep")

    def test_strips_quotes_and_whitespace(self):
        self.assertEqual(N("  Rock 'n'  Roll! "), "rock n roll")

    def test_unicode_apostrophe(self):
        self.assertEqual(N("Don’t Stop"), "dont stop")

    def test_diacritics_preserved(self):
        self.assertNotEqual(N("Väärä"), N("Vaara"))


class TestCreditSegments(unittest.TestCase):
    def test_comma_ampersand_list(self):
        self.assertEqual(C("Alok, Zeeba & Portugal. The Man"),
                         {"alok", "zeeba", "portugal the man"})

    def test_feat(self):
        self.assertIn("b", C("A feat. B"))
        self.assertIn("a", C("A feat. B"))

    def test_and_not_a_separator(self):
        self.assertIn("iron and wine", C("Iron and Wine"))


class TestTier1(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(T("Bush", "Sixteen Stone", "Bush", "Sixteen Stone"), 1)

    def test_case_insensitive(self):
        self.assertEqual(T("bush", "sixteen stone", "Bush", "Sixteen Stone"), 1)

    def test_punct_only_exact_still_tier1(self):
        self.assertEqual(T("XXXTentacion", "?", "XXXTentacion", "?"), 1)


class TestTier2(unittest.TestCase):
    def test_apostrophes(self):
        self.assertEqual(T("Lou Reed", "Rock N Roll Animal",
                           "Lou Reed", "Rock 'n' Roll Animal"), 2)

    def test_unicode_apostrophe(self):
        self.assertEqual(T("A", "Dont Stop", "A", "Don’t Stop"), 2)

    def test_trailing_period(self):
        self.assertEqual(T("A", "Vol 1", "A", "Vol. 1"), 2)

    def test_decorated_artist_exact_title(self):
        # bugs-style store decoration on the artist, literal title.
        self.assertEqual(T("Modest Mouse", "Good News",
                           "Modest Mouse(모디스트 마우스)", "Good News"), 2)


class TestTier3(unittest.TestCase):
    def test_remastered(self):
        self.assertEqual(T("Bush", "Sixteen Stone",
                           "Bush", "Sixteen Stone (Remastered)"), 3)

    def test_live_plus_punctuation(self):
        self.assertEqual(T("Lou Reed", "Rock N Roll Animal",
                           "Lou Reed", "Rock 'n' Roll Animal (Live)"), 3)

    def test_single_suffix(self):
        self.assertEqual(T("A", "Song", "A", "Song - Single"), 3)

    def test_reverse_direction(self):
        # Stream album decorated, store title plain (the "JOY - EP" case).
        self.assertEqual(T("Rhye", "JOY - EP", "Rhye", "JOY"), 3)

    def test_deluxe_bracket(self):
        self.assertEqual(T("A", "X", "A", "X [Super Deluxe Edition]"), 3)

    def test_delim_leading_query_legit(self):
        self.assertEqual(
            T("Oasis", "(What's the Story) Morning Glory?",
              "Oasis", "(What's the Story) Morning Glory? [Deluxe]"), 3)

    def test_collab_credit_live_case(self):
        self.assertEqual(T("Portugal. The Man", "Dive Into The Ocean",
                           "Alok, Zeeba & Portugal. The Man",
                           "Dive into the Ocean"), 3)

    def test_feat_credit(self):
        self.assertEqual(T("A", "Song", "A feat. B", "Song"), 3)

    def test_reverse_credit(self):
        self.assertEqual(T("A & B", "Song", "A", "Song"), 3)

    def test_credit_plus_decorated_title(self):
        self.assertEqual(T("Portugal. The Man", "Dive Into The Ocean",
                           "Alok, Zeeba & Portugal. The Man",
                           "Dive Into The Ocean - Single"), 3)


class TestTier0Rejections(unittest.TestCase):
    def test_wrong_artist(self):
        self.assertEqual(T("Rhye", "JOY - EP", "Alicia Keys", "No One - EP"), 0)

    def test_unrelated_title(self):
        self.assertEqual(T("Lou Reed", "Rock N Roll Animal",
                           "Lou Reed", "Transformer"), 0)

    def test_prefix_without_delimiter(self):
        self.assertEqual(T("A", "War", "A", "Warpaint"), 0)

    def test_diacritics_differ(self):
        self.assertEqual(T("A", "Väärä", "A", "Vaara"), 0)

    def test_empty_result_title(self):
        self.assertEqual(T("A", "X", "A", ""), 0)

    def test_artist_suffix_without_paren(self):
        self.assertEqual(T("Bush", "Sixteen Stone", "Bushido", "Sixteen Stone"), 0)

    def test_punct_only_query_album_no_vacuous_match(self):
        self.assertEqual(T("XXXTentacion", "?", "XXXTentacion", "(untitled)"), 0)

    def test_punct_only_artist_no_vacuous_match(self):
        self.assertEqual(T("!!!", "Wallop", "(G)I-DLE", "Wallop"), 0)

    def test_delim_leading_query_vs_punct_title(self):
        self.assertEqual(T("Oasis", "(What's the Story) Morning Glory?",
                           "Oasis", "?!"), 0)

    def test_credit_segment_substring_rejected(self):
        self.assertEqual(T("Bush", "Sixteen Stone", "Kate Bush", "Sixteen Stone"), 0)

    def test_credit_wrong_title(self):
        self.assertEqual(T("Portugal. The Man", "Dive Into The Ocean",
                           "Alok, Zeeba & Portugal. The Man", "Hearts"), 0)


if __name__ == "__main__":
    unittest.main()
