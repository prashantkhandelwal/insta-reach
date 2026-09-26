# Instagram Comment Collector

A Python Playwright script that attaches to an existing Chrome browser through the Chrome DevTools Protocol (CDP). It uses the attached browser's logged-in Instagram session, visits each post on a profile, expands rendered comments and replies, checkpoints progress, and exports CSV and JSON.

## Setup

```powershell
uv sync
```

Alternatively, create a virtual environment and run `python -m pip install -r requirements.txt`. Playwright connects to installed Chrome, so `playwright install` is not required for this workflow.

## Start Chrome

Close Chrome, then start a dedicated persistent Chrome profile with remote debugging enabled:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
	--remote-debugging-port=9222 `
	--user-data-dir="$env:LOCALAPPDATA\InstaReachChromeProfile"
```

Sign in to Instagram in that Chrome window. Chrome requires a non-default `--user-data-dir` for remote debugging; this directory preserves the login between runs.

## Collect

```powershell
uv run main username --keywords "launch,pricing" --max-posts 20
```

Use a username or full profile URL. Omit `--max-posts` (or use `0`) to scan until the final discovered post. After discovery, the attached Chrome window brings each post to the foreground, expands its comments and replies, and moves to the next post. By default, the script connects to `http://127.0.0.1:9222` and stores resumable progress in `.cache/instagram-comments.json`.

CSV and JSON files in `output/` are updated after every post, so comments already collected remain available if the script is interrupted. The script clicks Instagram's accessible **Load more comments** control, scrolls the internal comment panel until no additional comments load, and expands visible reply threads along the way. It snapshots comments after every load because Instagram may remove older rows from the DOM while scrolling; posts with many comments will therefore take longer.

Network traffic from profile discovery and comment traversal is saved to `output/<profile>-network.har`. The HAR includes response content for document, XHR, and Fetch requests so it can be inspected in Chrome DevTools. Cookie and authorization headers are redacted, but response bodies and URLs can still contain account or profile data; store the file securely.

Each row contains the profile name, follower count, post URL, caption, caption
keywords, post likes, comment author, comment text, comment keywords, and comment
likes. The follower count is read from the profile page and repeated on each
comment row. Post likes are read from the post's like label or metadata. Press
`Ctrl+C` to stop safely; rerun the same command to resume.

Use `--fresh` to discard cached progress before collecting the same profile again, especially after changing keywords or the post limit.

Useful options:

```text
--cdp-url URL     Chrome debugging endpoint
--output PATH     CSV and JSON output directory
--har PATH        Custom HAR output file
--cache PATH      Progress cache file
--fresh           Discard cached progress
--delay SECONDS   Delay between comment expansion interactions
--timeout SECONDS Navigation timeout
```

## Clean and transform

Run the cleanup script against a collected CSV:

```powershell
uv run python data-cleanup.py output/ai_drishti-comments.csv
```

The input CSV path is required. Optionally provide a second path for the cleaned
CSV, or pass `--in-place` to replace the input file.

The resulting `*-cleaned.csv` removes emoji-only comments, retains every original input column, and appends these analytics columns when they are not already present:

```text
creator_id, creator_type, follower_count, engagement_rate, comment_length,
comment_intensity, comment_sentiment_score, content_category,
audience_interaction_ratio, sentiment_distribution, hashtag_density
```

`comment_length` is the number of characters. Creator-level metrics are repeated
on each retained row for that creator and use each post's likes only once:

- `engagement_rate` is `(total likes + total retained comments) / followers * 100`.
- `comment_intensity` is `total retained comments / followers * 100`.
- `audience_interaction_ratio` is `total retained comments / total likes`.

These metrics are blank when their denominator is missing or zero.
`hashtag_density` is caption hashtag tokens divided by all caption tokens.
Creator type, follower count, sentiment, and content category are copied when
those columns exist in the source CSV; otherwise they are blank.

## Test

```powershell
uv run python -m unittest discover -s tests -v
```

Instagram changes its markup periodically, so selectors may need maintenance. Use collected data in accordance with Instagram's terms and applicable privacy rules.