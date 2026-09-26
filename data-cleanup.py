import argparse
import csv
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import emoji


OUTPUT_FIELDS = [
    "creator_id",
    "creator_type",
    "follower_count",
    "engagement_rate",
    "comment_length",
    "comment_intensity",
    "comment_sentiment_score",
    "content_category",
    "audience_interaction_ratio",
    "sentiment_distribution",
    "hashtag_density",
]


@dataclass
class CreatorTotals:
    comments: int = 0
    likes: float = 0
    followers: float | None = None


def is_emoji_only(comment: str) -> bool:
    return bool(emoji.emoji_list(comment)) and not emoji.replace_emoji(comment).strip()


def cleaned_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}-cleaned.csv")


def parse_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def format_ratio(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def hashtag_density(caption: str) -> str:
    tokens = caption.split()
    if not tokens:
        return ""
    hashtag_count = sum(bool(re.fullmatch(r"#\w+", token.rstrip(".,!?;:"))) for token in tokens)
    return format_ratio(hashtag_count / len(tokens))


def creator_key(row: dict[str, str]) -> str:
    return row.get("creator_id") or row.get("profile_name", "")


def calculate_creator_totals(
    rows: list[dict[str, str]],
    retained_rows: list[dict[str, str]],
) -> dict[str, CreatorTotals]:
    comments_per_creator = Counter(creator_key(row) for row in retained_rows)
    totals: dict[str, CreatorTotals] = {}
    posts_with_likes: dict[str, set[str]] = {}

    for row in rows:
        creator = creator_key(row)
        creator_totals = totals.setdefault(creator, CreatorTotals())
        creator_totals.comments = comments_per_creator[creator]

        followers = parse_number(row.get("follower_count"))
        if creator_totals.followers is None and followers is not None:
            creator_totals.followers = followers

        post_url = row.get("post_url", "")
        seen_posts = posts_with_likes.setdefault(creator, set())
        post_likes = parse_number(row.get("post_likes"))
        if post_url not in seen_posts and post_likes is not None:
            creator_totals.likes += post_likes
            seen_posts.add(post_url)

    return totals


def analytics_row(
    row: dict[str, str],
    totals_by_creator: dict[str, CreatorTotals],
) -> dict[str, str | int]:
    totals = totals_by_creator[creator_key(row)]

    engagement_rate = ""
    comment_intensity = ""
    if totals.followers is not None and totals.followers > 0:
        engagement_rate = format_ratio(
            (totals.likes + totals.comments) / totals.followers * 100
        )
        comment_intensity = format_ratio(
            totals.comments / totals.followers * 100
        )

    audience_interaction_ratio = ""
    if totals.likes > 0:
        audience_interaction_ratio = format_ratio(totals.comments / totals.likes)

    comment_text = row.get("comment_text", "") or ""
    return {
        "creator_id": creator_key(row),
        "creator_type": row.get("creator_type", ""),
        "follower_count": row.get("follower_count", ""),
        "engagement_rate": engagement_rate,
        "comment_length": len(comment_text),
        "comment_intensity": comment_intensity,
        "comment_sentiment_score": row.get("comment_sentiment_score", ""),
        "content_category": row.get("content_category", ""),
        "audience_interaction_ratio": audience_interaction_ratio,
        "sentiment_distribution": row.get("sentiment_distribution", ""),
        "hashtag_density": row.get("hashtag_density")
        or hashtag_density(row.get("post_caption", "")),
    }


def clean_comments(input_path: Path, output_path: Path) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    in_place = input_path.resolve() == output_path.resolve()
    temporary_path: Path | None = None

    if in_place:
        temporary = NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            suffix=".csv.tmp",
            dir=output_path.parent,
            delete=False,
        )
        destination = temporary
        temporary_path = Path(temporary.name)
    else:
        destination = output_path.open("w", encoding="utf-8", newline="")

    removed = 0
    kept = 0
    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as source, destination:
            reader = csv.DictReader(source)
            if not reader.fieldnames or "comment_text" not in reader.fieldnames:
                raise ValueError("CSV is missing the comment_text column")

            rows = list(reader)
            retained_rows = [
                row for row in rows if not is_emoji_only(row["comment_text"] or "")
            ]
            removed = len(rows) - len(retained_rows)
            totals_by_creator = calculate_creator_totals(rows, retained_rows)

            output_fields = [
                *reader.fieldnames,
                *(field for field in OUTPUT_FIELDS if field not in reader.fieldnames),
            ]
            writer = csv.DictWriter(destination, fieldnames=output_fields)
            writer.writeheader()
            for row in retained_rows:
                writer.writerow({**row, **analytics_row(row, totals_by_creator)})
            kept = len(retained_rows)

        if temporary_path is not None:
            os.replace(temporary_path, output_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return removed, kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove emoji-only comments and export comment analytics as CSV."
    )
    parser.add_argument("csv_file", type=Path, help="CSV file to clean")
    parser.add_argument("output", nargs="?", type=Path, help="optional output CSV file")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="replace the input CSV instead of creating a cleaned copy",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.in_place and args.output is not None:
        raise SystemExit("output cannot be supplied with --in-place")

    input_path = args.csv_file
    output_path = input_path if args.in_place else args.output or cleaned_output_path(input_path)
    removed, kept = clean_comments(input_path, output_path)
    print(f"Removed {removed} emoji-only comments; wrote {kept} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())