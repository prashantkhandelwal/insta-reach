from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse


RESERVED_PATHS = {"accounts", "direct", "explore", "p", "reel", "reels", "stories"}
CACHE_VERSION = 6
COMMENT_CONTROL = re.compile(
    r"view (?:all(?: \d+)? replies|more|previous|replies|\d+ more replies)|load more comments",
    re.IGNORECASE,
)
LOAD_MORE_COMMENTS = re.compile(r"^load more comments$", re.IGNORECASE)
VIEW_REPLIES = re.compile(
    r"^(?:view|load).*\brepl(?:y|ies)\b",
    re.IGNORECASE,
)
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie"}
BODY_RESOURCE_TYPES = {"Document", "Fetch", "XHR"}


def har_headers(headers: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "value": "[REDACTED]" if name.casefold() in SENSITIVE_HEADERS else str(value),
        }
        for name, value in headers.items()
    ]


class HarRecorder:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.entries: list[dict[str, Any]] = []
        self.pending: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        self.session.on("Network.requestWillBeSent", self._request_started)
        self.session.on("Network.responseReceived", self._response_received)
        self.session.on("Network.loadingFinished", self._request_finished)
        self.session.on("Network.loadingFailed", self._request_failed)
        self.session.send(
            "Network.enable",
            {
                "maxTotalBufferSize": 100_000_000,
                "maxResourceBufferSize": 10_000_000,
                "maxPostDataSize": 1_000_000,
            },
        )

    def _request_started(self, event: dict[str, Any]) -> None:
        request = event["request"]
        wall_time = event.get("wallTime", datetime.now(timezone.utc).timestamp())
        parsed_url = urlparse(request["url"])
        entry: dict[str, Any] = {
            "startedDateTime": datetime.fromtimestamp(wall_time, timezone.utc).isoformat().replace("+00:00", "Z"),
            "time": 0,
            "request": {
                "method": request["method"],
                "url": request["url"],
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": har_headers(request.get("headers", {})),
                "queryString": [
                    {"name": name, "value": value}
                    for name, value in parse_qsl(parsed_url.query, keep_blank_values=True)
                ],
                "headersSize": -1,
                "bodySize": len(request.get("postData", "").encode("utf-8")),
            },
            "response": {
                "status": 0,
                "statusText": "",
                "httpVersion": "",
                "cookies": [],
                "headers": [],
                "content": {"size": 0, "mimeType": ""},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
            "_resourceType": event.get("type", "Other"),
            "_startTimestamp": event.get("timestamp", 0),
        }
        if request.get("hasPostData") and request.get("postData") is not None:
            entry["request"]["postData"] = {
                "mimeType": request.get("headers", {}).get("content-type", ""),
                "text": request["postData"],
            }
        self.pending[event["requestId"]] = entry

    def _response_received(self, event: dict[str, Any]) -> None:
        entry = self.pending.get(event["requestId"])
        if not entry:
            return
        response = event["response"]
        entry["_resourceType"] = event.get("type", entry["_resourceType"])
        entry["response"].update(
            {
                "status": response.get("status", 0),
                "statusText": response.get("statusText", ""),
                "httpVersion": response.get("protocol", ""),
                "headers": har_headers(response.get("headers", {})),
                "content": {
                    "size": int(response.get("encodedDataLength", 0)),
                    "mimeType": response.get("mimeType", ""),
                },
                "redirectURL": response.get("headers", {}).get("location", ""),
                "bodySize": int(response.get("encodedDataLength", -1)),
            }
        )
        if response.get("remoteIPAddress"):
            entry["serverIPAddress"] = response["remoteIPAddress"]
        if response.get("connectionId") is not None:
            entry["connection"] = str(response["connectionId"])

    def _request_finished(self, event: dict[str, Any]) -> None:
        entry = self.pending.pop(event["requestId"], None)
        if not entry:
            return
        entry["time"] = max(0, (event.get("timestamp", 0) - entry.pop("_startTimestamp", 0)) * 1000)
        entry["response"]["bodySize"] = int(event.get("encodedDataLength", -1))
        entry["response"]["content"]["size"] = int(event.get("encodedDataLength", 0))
        if entry.pop("_resourceType", "") in BODY_RESOURCE_TYPES:
            try:
                body = self.session.send("Network.getResponseBody", {"requestId": event["requestId"]})
                entry["response"]["content"]["text"] = body.get("body", "")
                if body.get("base64Encoded"):
                    entry["response"]["content"]["encoding"] = "base64"
            except Exception:
                pass
        self.entries.append(entry)

    def _request_failed(self, event: dict[str, Any]) -> None:
        entry = self.pending.pop(event["requestId"], None)
        if not entry:
            return
        entry["time"] = max(0, (event.get("timestamp", 0) - entry.pop("_startTimestamp", 0)) * 1000)
        entry.pop("_resourceType", None)
        entry["response"]["_error"] = event.get("errorText", "Request failed")
        self.entries.append(entry)

    def save(self, path: Path) -> None:
        for entry in self.pending.values():
            entry.pop("_resourceType", None)
            entry.pop("_startTimestamp", None)
            self.entries.append(entry)
        self.pending.clear()
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "log": {
                "version": "1.2",
                "creator": {"name": "insta-reach", "version": "0.1.0"},
                "entries": self.entries,
            }
        }
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_count(value: str | None) -> int:
    text = (value or "").lower().replace(",", "").strip()
    match = re.search(r"([\d.]+)\s*([kmb])?", text)
    if not match:
        return 0
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(
        match.group(2), 1
    )
    return round(float(match.group(1)) * multiplier)


def parse_labeled_count(values: list[str], label: str) -> int | None:
    pattern = re.compile(
        rf"([\d,.]+\s*[kmb]?)\s+{re.escape(label)}s?\b",
        re.IGNORECASE,
    )
    for value in values:
        if match := pattern.search(value):
            return parse_count(match.group(1))
    return None


def parse_post_likes(values: list[str]) -> int | None:
    likes = parse_labeled_count(values, "like")
    if likes is not None:
        return likes

    others = parse_labeled_count(values, "other")
    return others + 1 if others is not None else None


def read_follower_count(page: Any) -> int | None:
    candidates = page.evaluate(
        r"""() => {
            const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
            const link = document.querySelector("a[href$='/followers/']");
            const values = link
                ? [
                    link.textContent,
                    link.getAttribute('title'),
                    link.getAttribute('aria-label'),
                    ...[...link.querySelectorAll('[title], [aria-label]')]
                        .flatMap(element => [
                            element.getAttribute('title'),
                            element.getAttribute('aria-label'),
                        ]),
                ]
                : [];
            values.push(
                document.querySelector("meta[property='og:description']")
                    ?.getAttribute('content')
            );
            return values.map(normalize).filter(Boolean);
        }"""
    )
    return parse_labeled_count(candidates, "follower")


def extract_keywords(text: str, requested_keywords: list[str]) -> list[str]:
    normalized = " ".join(text.lower().split())
    discovered = re.findall(r"(?:#|@)[^\W]+(?:[._][^\W]+)*", normalized, re.UNICODE)
    requested = [
        keyword.strip().lower()
        for keyword in requested_keywords
        if keyword.strip() and keyword.strip().lower() in normalized
    ]
    return list(dict.fromkeys([*requested, *discovered]))


def normalize_author(value: str) -> str:
    return re.sub(r"Verified$", "", value.strip(), flags=re.IGNORECASE).strip()


def normalize_profile_url(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://www.instagram.com/{candidate.strip('/')}/"
    parsed = urlparse(candidate)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("The profile must be on www.instagram.com.")
    if len(parts) != 1 or parts[0].lower() in RESERVED_PATHS:
        raise ValueError("Provide an Instagram profile URL or username, not a post URL.")
    username = parts[0]
    return urlunparse(("https", "www.instagram.com", f"/{username}/", "", "", "")), username


def canonicalize_post_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part.lower() in {"p", "reel"} and index + 1 < len(parts):
            prefix = f"{parts[index - 1]}/" if index == 1 else ""
            return f"https://www.instagram.com/{prefix}{part.lower()}/{parts[index + 1]}/"
    return None


def load_cache(path: Path, profile_url: str) -> dict[str, Any]:
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("profile_url") == profile_url:
            cached["post_urls"] = list(
                dict.fromkeys(
                    canonical
                    for value in cached.get("post_urls", [])
                    if (canonical := canonicalize_post_url(value)) is not None
                )
            )
            cached["completed_posts"] = list(
                dict.fromkeys(
                    canonical
                    for value in cached.get("completed_posts", [])
                    if (canonical := canonicalize_post_url(value)) is not None
                )
            )
            cached.setdefault("failed_posts", [])
            if cached.get("cache_version") != CACHE_VERSION:
                cached["completed_posts"] = []
                cached["failed_posts"] = []
                cached["comments"] = []
            cached.setdefault("follower_count", None)
            cached["cache_version"] = CACHE_VERSION
            return cached
    return {
        "cache_version": CACHE_VERSION,
        "profile_url": profile_url,
        "post_urls": [],
        "completed_posts": [],
        "failed_posts": [],
        "comments": [],
        "follower_count": None,
        "status": "new",
    }


def save_cache(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def discover_posts(page: Any, max_posts: int, delay: float) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    unchanged_rounds = 0
    previous_height = 0

    while unchanged_rounds < 5 and (max_posts == 0 or len(urls) < max_posts):
        hrefs = page.locator("a[href*='/p/'], a[href*='/reel/']").evaluate_all(
            "elements => elements.map(element => element.href.split('?')[0])"
        )
        for href in hrefs:
            canonical = canonicalize_post_url(href)
            if canonical and canonical not in seen:
                seen.add(canonical)
                urls.append(canonical)
        height = page.evaluate("document.documentElement.scrollHeight")
        unchanged_rounds = unchanged_rounds + 1 if height == previous_height else 0
        previous_height = height
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(round(delay * 1000))

    return urls[:max_posts] if max_posts else urls


def comment_scroll_metrics(page: Any) -> dict[str, int | bool]:
    return page.locator("main").first.evaluate(
    r"""main => {
                    const visibleTimes = [...main.querySelectorAll('time')]
                        .filter(time => time.getClientRects().length > 0);
                    let container = visibleTimes[0]?.parentElement || null;
                    while (container && container !== main) {
                        const style = getComputedStyle(container);
                        if (container.scrollHeight > container.clientHeight + 20 &&
                                ['auto', 'scroll'].includes(style.overflowY)) break;
                        container = container.parentElement;
                    }
                    if (!container || container === main) {
                        container = [...main.querySelectorAll('*')]
                            .filter(element => element.clientHeight > 100 && element.scrollHeight > element.clientHeight + 20)
                            .sort((left, right) => right.scrollHeight - left.scrollHeight)[0] || null;
                    }
                    if (container) container.scrollTop = container.scrollHeight;
                    return {
                        found: Boolean(container),
                        rowCount: main.querySelectorAll('time').length,
                        scrollHeight: container?.scrollHeight || 0,
                    };
                }"""
            )


def click_comment_controls(page: Any) -> int:
    clicked = 0
    load_more = page.get_by_role("button", name=LOAD_MORE_COMMENTS)
    if load_more.count():
        try:
            load_more.first.click(timeout=2_000)
            clicked += 1
        except Exception:
            pass

    replies = page.get_by_role("button", name=VIEW_REPLIES)
    for index in range(replies.count() - 1, -1, -1):
        control = replies.nth(index)
        try:
            if control.is_visible() and control.is_enabled():
                control.click(timeout=1_000)
                clicked += 1
        except Exception:
            continue
    return clicked


def merge_comment_rows(
    collected: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        identity = row.get("comment_id") or (
            f"{row['comment_author'].casefold()}\n{row['comment_text']}"
        )
        collected[identity] = row


def expand_comments(
    page: Any,
    delay: float,
    profile_name: str,
    follower_count: int | None,
    requested_keywords: list[str],
) -> tuple[int, list[dict[str, Any]]]:
    idle_rounds = 0
    passes = 0
    previous_signature = (-1, -1)
    row_count = 0
    collected: dict[str, dict[str, Any]] = {}
    while idle_rounds < 8 and passes < 1_000:
        passes += 1
        merge_comment_rows(
            collected,
            read_post(page, profile_name, follower_count, requested_keywords),
        )
        clicked = click_comment_controls(page)
        if clicked:
            page.wait_for_timeout(round(max(delay, 0.75) * 1000))
            merge_comment_rows(
                collected,
                read_post(page, profile_name, follower_count, requested_keywords),
            )
        metrics = comment_scroll_metrics(page)
        row_count = int(metrics["rowCount"])
        signature = (row_count, int(metrics["scrollHeight"]))
        idle_rounds = idle_rounds + 1 if signature == previous_signature else 0
        previous_signature = signature
        page.wait_for_timeout(round(max(delay, 1.0) * 1000))
    merge_comment_rows(
        collected,
        read_post(page, profile_name, follower_count, requested_keywords),
    )
    return row_count, list(collected.values())


def open_comments(page: Any, delay: float) -> None:
    icon = page.locator("svg[aria-label='Comment']").first
    if not icon.count() or not icon.is_visible():
        return
    control = icon.locator("xpath=ancestor::*[@role='button' or self::button][1]")
    if control.count():
        control.click(timeout=2_000)
    else:
        icon.click(timeout=2_000)
    page.wait_for_timeout(round(max(delay, 1.0) * 1000))


def read_post(
    page: Any,
    profile_name: str,
    follower_count: int | None,
    requested_keywords: list[str],
) -> list[dict[str, Any]]:
    root = page.locator("article").first
    legacy_layout = bool(root.count())
    if not legacy_layout:
        root = page.locator("main").first
    data = root.evaluate(
                r"""(root, options) => {
          const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const pickText = (element, author, timestamp) => [...element.querySelectorAll('span')]
                        .map(span => normalize(span.textContent))
                        .filter(value => value && value !== author && value !== timestamp)
                        .filter(value => !/^(follow|reply|likes?|see translation|… more|more)$/i.test(value))
                        .filter(value => !/^[\d,.]+[kmb]?$/i.test(value))
                        .sort((left, right) => right.length - left.length)[0] || '';
                    let caption = '';
                    let comments = [];

                    if (options.legacyLayout) {
                        const items = [...root.querySelectorAll('ul li')];
                        const captionSpans = items[0] ? [...items[0].querySelectorAll('span')].map(span => normalize(span.textContent)) : [];
                        caption = captionSpans.sort((left, right) => right.length - left.length)[0] || '';
                        comments = items.slice(1).map(item => {
                            const author = normalize(item.querySelector("a[href^='/']")?.textContent);
                            const text = pickText(item, author, '');
                            const commentLikes = normalize(item.textContent).match(/([\d.,]+\s*[kmb]?)\s+likes?/i)?.[1] || '';
                            return author && text ? { author, text, likesText: commentLikes } : null;
                        }).filter(Boolean);
                    } else {
                        const records = [...root.querySelectorAll('time')].map(time => {
                            const timestamp = normalize(time.textContent);
                            let row = time.parentElement;
                            const permalink = time.closest('a')?.getAttribute('href') || '';
                            const commentId = permalink.match(/\/c\/([^/]+)/)?.[1] || '';
                            while (row && row !== root) {
                                const authorLink = [...row.querySelectorAll("a[href^='/']")]
                                    .find(link => normalize(link.textContent) && /^\/[^/]+\/$/.test(link.getAttribute('href') || ''));
                                const author = normalize(authorLink?.textContent);
                                const text = pickText(row, author, timestamp);
                                if (author && text) {
                                    const commentLikes = normalize(row.textContent).match(/([\d.,]+\s*[kmb]?)\s+likes?/i)?.[1] || '';
                                      return { author, text, likesText: commentLikes, commentId };
                                }
                                row = row.parentElement;
                            }
                            return null;
                        }).filter(Boolean);
                        const unique = [...new Map(records.map(record => [`${record.author}\n${record.text}`, record])).values()];
                        const captionIndex = unique.findIndex(record => record.author.toLowerCase() === options.profileName.toLowerCase());
                        if (captionIndex >= 0) caption = unique.splice(captionIndex, 1)[0].text;
                        comments = unique;
                    }

                    const elementText = element => [
                        normalize(element.textContent),
                        normalize(element.getAttribute('aria-label')),
                        normalize(element.getAttribute('title')),
                    ];
                    const likesTexts = [
                        ...root.querySelectorAll("a[href$='/liked_by/']"),
                    ].flatMap(elementText);
                    likesTexts.push(normalize(
                        document.querySelector("meta[property='og:description']")
                            ?.getAttribute('content')
                    ));
                    likesTexts.push(
                        ...[...root.querySelectorAll(
                            "section button, section span, section a"
                        )].flatMap(elementText)
                    );
                    return {
                        caption,
                        likesTexts: likesTexts.filter(Boolean),
                        comments,
                    };
                }""",
                {"legacyLayout": legacy_layout, "profileName": profile_name},
    )
    post_url = page.url.split("?")[0]
    post_keywords = extract_keywords(data["caption"], requested_keywords)
    post_likes = parse_post_likes(data["likesTexts"])
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for comment in data["comments"]:
        author = normalize_author(comment["author"])
        text = comment["text"].strip()
        if author.casefold() == profile_name.casefold() and text.casefold() == profile_name.casefold():
            continue
        identity = (author, text)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "profile_name": profile_name,
                "follower_count": follower_count if follower_count is not None else "",
                "post_url": post_url,
                "post_caption": data["caption"],
                "post_keywords": post_keywords,
                "post_likes": post_likes,
                "comment_author": author,
                "comment_text": text,
                "comment_id": comment.get("commentId", ""),
                "comment_keywords": extract_keywords(text, requested_keywords),
                "comment_likes": parse_count(comment["likesText"]),
            }
        )
    return rows


def export_results(output_dir: Path, profile_name: str, comments: list[dict[str, Any]]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{profile_name}-comments.json"
    csv_path = output_dir / f"{profile_name}-comments.csv"
    json_path.write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
    headers = list(comments[0]) if comments else [
        "profile_name", "follower_count", "post_url", "post_caption",
        "post_keywords", "post_likes", "comment_author", "comment_text",
        "comment_keywords", "comment_likes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        for comment in comments:
            writer.writerow({key: " | ".join(value) if isinstance(value, list) else value for key, value in comment.items()})
    return json_path, csv_path


def persist_progress(
    cache_path: Path,
    output_dir: Path,
    profile_name: str,
    state: dict[str, Any],
) -> tuple[Path, Path]:
    save_cache(cache_path, state)
    return export_results(output_dir, profile_name, state["comments"])


def collect(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    profile_url, profile_name = normalize_profile_url(args.profile)
    cache_path = Path(args.cache)
    if args.fresh:
        cache_path.unlink(missing_ok=True)
    state = load_cache(cache_path, profile_url)
    requested_keywords = [value.strip() for value in args.keywords.split(",") if value.strip()]
    output_dir = Path(args.output)
    har_path = Path(args.har) if args.har else output_dir / f"{profile_name}-network.har"
    playwright = sync_playwright().start()
    worker_page = None
    cdp_session = None
    har_recorder = None

    try:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        if not browser.contexts:
            raise RuntimeError("Chrome exposed no browser context over CDP.")
        context = browser.contexts[0]
        worker_page = context.new_page()
        cdp_session = context.new_cdp_session(worker_page)
        har_recorder = HarRecorder(cdp_session)
        har_recorder.start()
        worker_page.bring_to_front()
        worker_page.goto(profile_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        worker_page.wait_for_selector("main", timeout=args.timeout * 1000)
        follower_count = read_follower_count(worker_page)
        if follower_count is None:
            follower_count = state.get("follower_count")
        if follower_count is None:
            print(
                "Follower count was not found on the profile; it will be blank in the export.",
                file=sys.stderr,
            )
        else:
            state["follower_count"] = follower_count
            print(f"Profile followers: {follower_count:,}")

        if not state["post_urls"]:
            print(f"Discovering posts on @{profile_name}...")
            state["post_urls"] = discover_posts(worker_page, args.max_posts, args.delay)
            state["status"] = "collecting"
            persist_progress(cache_path, output_dir, profile_name, state)
        if not state["post_urls"]:
            raise RuntimeError("No posts were found. Confirm the attached Chrome profile can view this account.")

        completed = set(state["completed_posts"])
        pending = [url for url in state["post_urls"] if url not in completed]
        state["status"] = "collecting"
        state.pop("error", None)
        for index, post_url in enumerate(pending, start=len(completed) + 1):
            print(f"[{index}/{len(state['post_urls'])}] Opening {post_url}")
            try:
                worker_page.bring_to_front()
                worker_page.goto(post_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                worker_page.wait_for_selector("main", timeout=args.timeout * 1000)
                open_comments(worker_page, args.delay)
                loaded_rows, comments = expand_comments(
                    worker_page,
                    args.delay,
                    profile_name,
                    follower_count,
                    requested_keywords,
                )
                state["comments"].extend(comments)
                state["completed_posts"].append(post_url)
                state["failed_posts"] = [
                    failure for failure in state["failed_posts"]
                    if failure.get("post_url") != post_url
                ]
                print(
                    f"  Loaded {loaded_rows} comment rows; saved {len(comments)} comments "
                    f"({len(state['comments'])} total)."
                )
            except Exception as error:
                state["failed_posts"] = [
                    failure for failure in state["failed_posts"]
                    if failure.get("post_url") != post_url
                ]
                state["failed_posts"].append({"post_url": post_url, "error": str(error)})
                print(f"  Skipped: {error}", file=sys.stderr)
            persist_progress(cache_path, output_dir, profile_name, state)

        state["status"] = "complete_with_errors" if state["failed_posts"] else "complete"
        json_path, csv_path = persist_progress(
            cache_path, output_dir, profile_name, state
        )
        print(f"Collected {len(state['comments'])} comments.")
        print(f"JSON: {json_path.resolve()}")
        print(f"CSV:  {csv_path.resolve()}")
        return 0
    except PlaywrightTimeoutError as error:
        state["status"] = "error"
        state["error"] = str(error)
        save_cache(cache_path, state)
        print("Instagram did not finish loading. Progress remains cached.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        state["status"] = "stopped"
        save_cache(cache_path, state)
        print("Stopped. Progress remains cached.", file=sys.stderr)
        return 130
    except Exception as error:
        state["status"] = "error"
        state["error"] = str(error)
        save_cache(cache_path, state)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if har_recorder is not None:
            try:
                if worker_page is not None:
                    worker_page.wait_for_timeout(500)
                har_recorder.save(har_path)
                print(f"HAR:  {har_path.resolve()}")
            except Exception as error:
                print(f"Could not save HAR: {error}", file=sys.stderr)
        if cdp_session is not None:
            try:
                cdp_session.detach()
            except Exception:
                pass
        if worker_page is not None:
            try:
                worker_page.close()
            except Exception:
                pass
        playwright.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect rendered comments from an Instagram profile.")
    parser.add_argument("profile", help="Instagram username or profile URL")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="Chrome remote debugging URL")
    parser.add_argument("--keywords", default="", help="Comma-separated keywords to match")
    parser.add_argument("--max-posts", type=int, default=0, help="Maximum posts; 0 means all discovered posts")
    parser.add_argument("--output", default="output", help="Export directory")
    parser.add_argument("--har", help="HAR output path; defaults to output/<profile>-network.har")
    parser.add_argument("--cache", default=".cache/instagram-comments.json", help="Progress cache file")
    parser.add_argument("--fresh", action="store_true", help="Discard cached progress and start again")
    parser.add_argument("--delay", type=float, default=0.8, help="Seconds between page interactions")
    parser.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_posts < 0 or args.delay < 0 or args.timeout <= 0:
        print("--max-posts and --delay must be non-negative; --timeout must be positive.", file=sys.stderr)
        return 2
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())