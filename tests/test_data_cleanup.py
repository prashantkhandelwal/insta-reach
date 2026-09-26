import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "data-cleanup.py"
SPEC = importlib.util.spec_from_file_location("data_cleanup", MODULE_PATH)
assert SPEC and SPEC.loader
data_cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(data_cleanup)


class DataCleanupTests(unittest.TestCase):
    def test_cli_requires_input_csv(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH)],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("the following arguments are required: csv_file", result.stderr)

    def test_clean_comments_exports_requested_analytics_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "comments.csv"
            output_path = Path(directory) / "cleaned.csv"
            with input_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=[
                        "profile_name",
                        "post_url",
                        "post_caption",
                        "post_likes",
                        "comment_text",
                        "follower_count",
                    ],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "profile_name": "creator",
                            "post_url": "post-1",
                            "post_caption": "A #tag caption",
                            "post_likes": "10",
                            "comment_text": "Great!",
                            "follower_count": "100",
                        },
                        {
                            "profile_name": "creator",
                            "post_url": "post-1",
                            "post_caption": "A #tag caption",
                            "post_likes": "10",
                            "comment_text": "Nice post",
                            "follower_count": "100",
                        },
                        {
                            "profile_name": "creator",
                            "post_url": "post-1",
                            "post_caption": "A #tag caption",
                            "post_likes": "10",
                            "comment_text": "🔥🔥",
                            "follower_count": "100",
                        },
                        {
                            "profile_name": "creator",
                            "post_url": "post-2",
                            "post_caption": "Another post",
                            "post_likes": "30",
                            "comment_text": "Excellent",
                            "follower_count": "100",
                        },
                    ]
                )

            removed, kept = data_cleanup.clean_comments(input_path, output_path)

            with output_path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)

            self.assertEqual((removed, kept), (1, 3))
            self.assertEqual(
                reader.fieldnames,
                [
                    "profile_name",
                    "post_url",
                    "post_caption",
                    "post_likes",
                    "comment_text",
                    "follower_count",
                    *(
                        field
                        for field in data_cleanup.OUTPUT_FIELDS
                        if field != "follower_count"
                    ),
                ],
            )
            self.assertEqual(rows[0]["comment_text"], "Great!")
            self.assertEqual(rows[0]["creator_id"], "creator")
            self.assertEqual(rows[0]["comment_length"], "6")
            self.assertEqual(rows[0]["engagement_rate"], "43")
            self.assertEqual(rows[0]["comment_intensity"], "3")
            self.assertEqual(rows[0]["audience_interaction_ratio"], "0.075")
            self.assertEqual(rows[0]["hashtag_density"], "0.333333")
            self.assertEqual(rows[0]["comment_sentiment_score"], "")
            self.assertTrue(
                all(row["engagement_rate"] == "43" for row in rows)
            )


if __name__ == "__main__":
    unittest.main()