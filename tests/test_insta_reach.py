import json
import tempfile
import unittest
from pathlib import Path

from insta_reach import (
    COMMENT_CONTROL,
    HarRecorder,
    VIEW_REPLIES,
    canonicalize_post_url,
    extract_keywords,
    load_cache,
    merge_comment_rows,
    normalize_author,
    normalize_profile_url,
    parse_count,
    persist_progress,
    save_cache,
)


class InstagramCollectorTests(unittest.TestCase):
    def test_har_recorder_writes_response_content_and_redacts_headers(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.handlers = {}

            def on(self, event, handler) -> None:
                self.handlers[event] = handler

            def send(self, method, params=None):
                if method == "Network.getResponseBody":
                    return {"body": '{"comments": []}', "base64Encoded": False}
                return {}

        session = FakeSession()
        recorder = HarRecorder(session)
        recorder.start()
        session.handlers["Network.requestWillBeSent"](
            {
                "requestId": "request-1",
                "timestamp": 10.0,
                "wallTime": 1_700_000_000,
                "type": "Fetch",
                "request": {
                    "method": "GET",
                    "url": "https://www.instagram.com/api/comments?after=1",
                    "headers": {"Cookie": "secret", "Authorization": "token"},
                },
            }
        )
        session.handlers["Network.responseReceived"](
            {
                "requestId": "request-1",
                "type": "Fetch",
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "protocol": "h2",
                    "headers": {"content-type": "application/json"},
                    "mimeType": "application/json",
                    "encodedDataLength": 16,
                },
            }
        )
        session.handlers["Network.loadingFinished"](
            {"requestId": "request-1", "timestamp": 10.25, "encodedDataLength": 16}
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network.har"
            recorder.save(path)
            har = json.loads(path.read_text(encoding="utf-8"))

        entry = har["log"]["entries"][0]
        self.assertEqual(har["log"]["version"], "1.2")
        self.assertEqual(entry["response"]["content"]["text"], '{"comments": []}')
        self.assertEqual(
            [header["value"] for header in entry["request"]["headers"]],
            ["[REDACTED]", "[REDACTED]"],
        )

    def test_merge_comment_rows_keeps_virtualized_comments(self) -> None:
        collected = {}
        first = {
            "comment_id": "1",
            "comment_author": "first_user",
            "comment_text": "First comment",
            "comment_likes": 0,
        }
        last = {
            "comment_id": "2",
            "comment_author": "last_user",
            "comment_text": "Last comment",
            "comment_likes": 0,
        }

        merge_comment_rows(collected, [first])
        merge_comment_rows(collected, [last])

        self.assertEqual(list(collected.values()), [first, last])

    def test_comment_control_matches_numbered_reply_button(self) -> None:
        self.assertIsNotNone(COMMENT_CONTROL.search("View all 12 replies"))
        self.assertIsNotNone(COMMENT_CONTROL.search("Load more comments"))

    def test_reply_control_supports_singular_labels(self) -> None:
        self.assertIsNotNone(VIEW_REPLIES.search("View 1 more reply"))

    def test_normalize_author_removes_verified_label(self) -> None:
        self.assertEqual(normalize_author("kyraonigVerified"), "kyraonig")

    def test_canonicalize_post_url_preserves_profile_prefix(self) -> None:
        self.assertEqual(
            canonicalize_post_url("https://www.instagram.com/kyraonig/reel/DUz3a5Lj_sa/?x=1"),
            "https://www.instagram.com/kyraonig/reel/DUz3a5Lj_sa/",
        )
        self.assertEqual(
            canonicalize_post_url("https://www.instagram.com/p/Db52w4sjyfk/"),
            "https://www.instagram.com/p/Db52w4sjyfk/",
        )

    def test_parse_count_supports_abbreviations(self) -> None:
        self.assertEqual(parse_count("1.2K likes"), 1200)
        self.assertEqual(parse_count("2,345 likes"), 2345)
        self.assertEqual(parse_count("Like"), 0)

    def test_extract_keywords_deduplicates_matches(self) -> None:
        self.assertEqual(
            extract_keywords("Great Launch! #Product by @Maker #product", ["launch", "missing"]),
            ["launch", "#product", "@maker"],
        )

    def test_normalize_profile_url_rejects_post_urls(self) -> None:
        self.assertEqual(
            normalize_profile_url("example"),
            ("https://www.instagram.com/example/", "example"),
        )
        with self.assertRaises(ValueError):
            normalize_profile_url("https://www.instagram.com/p/post-id/")

    def test_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            state = load_cache(path, "https://www.instagram.com/example/")
            state["comments"].append({"comment_text": "hello"})
            save_cache(path, state)
            self.assertEqual(load_cache(path, state["profile_url"]), state)

    def test_old_cache_is_reset_for_complete_comment_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(
                json.dumps(
                    {
                        "profile_url": "https://www.instagram.com/example/",
                        "post_urls": ["https://www.instagram.com/example/p/one/"],
                        "completed_posts": ["https://www.instagram.com/example/p/one/"],
                        "comments": [{"comment_text": "partial"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_cache(path, "https://www.instagram.com/example/")

            self.assertEqual(state["completed_posts"], [])
            self.assertEqual(state["comments"], [])

    def test_persist_progress_writes_cache_csv_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "cache.json"
            state = load_cache(cache_path, "https://www.instagram.com/example/")
            state["comments"].append(
                {
                    "profile_name": "example",
                    "post_url": "https://www.instagram.com/p/one/",
                    "post_caption": "Caption",
                    "post_keywords": ["caption"],
                    "post_likes": 10,
                    "comment_author": "viewer",
                    "comment_text": "Comment",
                    "comment_keywords": [],
                    "comment_likes": 2,
                }
            )

            json_path, csv_path = persist_progress(
                cache_path, root / "output", "example", state
            )

            self.assertTrue(cache_path.exists())
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), state["comments"])
            self.assertIn("viewer", csv_path.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()