"""Comment variety: the example is an idea, not a word bank.

A real run put "paldo" in roughly fifteen of twenty comments under one post.
Three things caused it and each is pinned here:
  * the prompt asked for the examples' slang back,
  * the quality gate REQUIRED one of the examples' words or it rejected the
    comment, so repetition was mandatory,
  * nothing counted how often a word had already gone out.

Pure functions only - no network, no model, no phones.
"""

import unittest

import core


class WordKeyTests(unittest.TestCase):
    def test_variants_of_one_word_share_one_budget(self):
        """paldo / paldong / paldooo are one word as far as the cap cares."""
        for variant in ("paldo", "paldong", "Paldong", "PALDO",
                        "paldoo", "paldooo"):
            with self.subTest(variant=variant):
                self.assertEqual(core._word_key(variant), "paldo")

    def test_doubled_consonants_are_left_alone(self):
        self.assertEqual(core._word_key("ball"), "ball")

    def test_repeated_word_tokenises_to_one_key(self):
        self.assertEqual(core._content_keys("paldo-paldo"), {"paldo"})


class WordBudgetTests(unittest.TestCase):
    def setUp(self):
        core._pool_by_link.clear()
        core.set_current_post_url("https://www.facebook.com/reel/testpost")
        self.addCleanup(core._pool_by_link.clear)

    def test_a_word_is_retired_after_its_allowance(self):
        for i in range(core.WORD_USE_LIMIT):
            self.assertNotIn("paldo", core._spent_words(),
                             f"retired too early, after {i} uses")
            core._claim_words("paldo talaga sila")
        self.assertIn("paldo", core._spent_words())

    def test_the_budget_is_per_post(self):
        for _ in range(core.WORD_USE_LIMIT):
            core._claim_words("paldo talaga sila")
        self.assertIn("paldo", core._spent_words())
        # A different link starts with a clean vocabulary - otherwise ten links
        # share one budget and the wording runs out mid-run.
        other = "https://www.facebook.com/reel/another"
        self.assertNotIn("paldo", core._spent_words(other))


class QualityGateTests(unittest.TestCase):
    EXAMPLES = ["Paldong paldo talaga ang mag ina"]
    CTX = "Isang post tungkol sa bilyones ng pulitiko habang naghihirap ang bayan"

    def setUp(self):
        core._pool_by_link.clear()
        core.set_current_post_url("https://www.facebook.com/reel/gatepost")
        self.addCleanup(core._pool_by_link.clear)

    def _gate(self, text):
        return core._good_context_comment(text, self.EXAMPLES, self.CTX)

    def test_own_words_on_the_same_topic_are_accepted(self):
        """The old gate rejected this for borrowing none of the example's
        words. Writing it fresh is the whole point, so it must pass now."""
        self.assertTrue(self._gate("sobrang yaman nila habang gutom ang bayan"))

    def test_echoing_several_example_words_is_rejected(self):
        self.assertFalse(self._gate("paldo talaga ang mag ina nila"))

    def test_a_retired_word_is_rejected(self):
        for _ in range(core.WORD_USE_LIMIT):
            core._claim_words("paldo nanaman sila")
        self.assertIn("paldo", core._spent_words())
        self.assertFalse(self._gate("paldo nanaman ang mayayaman"))
        # ...while the same thought in other words still gets through.
        self.assertTrue(self._gate("ang yaman nila sobrang lupit"))

    def test_handing_the_example_straight_back_is_rejected(self):
        self.assertFalse(self._gate(self.EXAMPLES[0]))


class ScreenshotReplayTests(unittest.TestCase):
    """Replay the comments from the reported run; most must now be stopped."""

    REAL = [
        "Grabe paldo ng mga mag ina eh yung bilyones nila tas tayo naghihirap",
        "Paldong paldo talaga sila mag ina habang tayo naghihirap",
        "Paldong paldong mag ina pero bayan nagugutom eh",
        "Grabe naman talaga yung paldong paldo mag ina",
        "paldooo nanaman c solar boy",
        "ugali na ata nila yan mag ina masiba sa pera",
    ]

    def setUp(self):
        core._pool_by_link.clear()
        core.set_current_post_url("https://www.facebook.com/reel/replay")
        self.addCleanup(core._pool_by_link.clear)

    def test_the_run_stops_repeating_after_the_allowance(self):
        allowed = []
        for text in self.REAL:
            if core._spent_words() & core._content_keys(text):
                continue                      # the cap would reject this one
            core._claim_words(text)
            allowed.append(text)
        # Only the first few survive; the rest are the repetition complained of.
        self.assertEqual(len(allowed), core.WORD_USE_LIMIT)
        self.assertLess(len(allowed), len(self.REAL))
        for word in ("paldo", "mag", "ina"):
            self.assertIn(word, core._spent_words())


if __name__ == "__main__":
    unittest.main()
