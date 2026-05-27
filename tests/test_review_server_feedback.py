import csv
import os
import tempfile
import unittest

from review_server import update_keyword_scores_for_feedback
from review_server import load_agenda_items


class ReviewServerFeedbackTest(unittest.TestCase):
    def _write_scores(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["keyword", "priority_score", "examples", "updated_at"])
            writer.writeheader()
            writer.writerow({"keyword": "land division", "priority_score": "70", "examples": "", "updated_at": ""})

    def test_wrong_should_be_higher_increases_matching_keyword_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            score_path = os.path.join(tmp, "keyword_scores.csv")
            self._write_scores(score_path)

            result = update_keyword_scores_for_feedback(
                text="Plan commission request to seek legal counsel land division",
                current_priority="low",
                feedback="higher",
                score_path=score_path,
            )

            self.assertEqual(result["matched_keywords"], ["land division"])
            self.assertEqual(result["delta"], 8)

            with open(score_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(rows[0]["priority_score"], "78")

    def test_agenda_items_include_local_file_and_source_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            medium_path = os.path.join(tmp, "medium.csv")
            with open(medium_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["filename", "item", "importance", "original_text"])
                writer.writeheader()
                writer.writerow({
                    "filename": "Winneconne_Town_Board_2026-05-21.pdf",
                    "item": "Budget resolution",
                    "importance": "medium",
                    "original_text": "6. Budget resolution",
                })

            items = load_agenda_items(
                agenda_csvs=[("medium", medium_path)],
                source_lookup={"Winneconne_Town_Board": "https://example.com/agendas"},
            )

            self.assertEqual(items[0]["local_file_url"], "/files/Winneconne_Town_Board_2026-05-21.pdf")
            self.assertEqual(items[0]["source_url"], "https://example.com/agendas")


if __name__ == "__main__":
    unittest.main()
