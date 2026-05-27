import sys
import types
import unittest


class _FakeOpenAI:
    def __init__(self, *args, **kwargs):
        pass


sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))

from analyze_agendas import classify_importance, score_text_with_keywords


class KeywordScoringTest(unittest.TestCase):
    def test_keyword_score_can_promote_text_to_high(self):
        scores = [{"keyword": "land division", "priority_score": 88}]

        self.assertEqual(
            classify_importance("Plan commission request to seek legal counsel land division", scores),
            "high",
        )

    def test_returns_best_matching_keyword_score(self):
        scores = [
            {"keyword": "report", "priority_score": 10},
            {"keyword": "ordinance", "priority_score": 65},
        ]

        score, matched = score_text_with_keywords("Budget report and ordinance update", scores)

        self.assertEqual(score, 65)
        self.assertEqual(matched, ["ordinance", "report"])


if __name__ == "__main__":
    unittest.main()
