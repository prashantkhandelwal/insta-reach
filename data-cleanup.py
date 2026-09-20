import argparse
import csv
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import emoji


DEFAULT_INPUT = Path("output/ai_drishti-comments.csv")


def is_emoji_only(comment: str) -> bool:
    return bool(emoji.emoji_list(comment)) and not emoji.replace_emoji(comment).strip()


def cleaned_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}-cleaned.csv")


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

            writer = csv.DictWriter(destination, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if is_emoji_only(row["comment_text"] or ""):
                    removed += 1
                    continue
                writer.writerow(row)
                kept += 1

        if temporary_path is not None:
            os.replace(temporary_path, output_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return removed, kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove emoji-only comments from a CSV file.")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("output", nargs="?", type=Path)
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

    output_path = args.input if args.in_place else args.output or cleaned_output_path(args.input)
    removed, kept = clean_comments(args.input, output_path)
    print(f"Removed {removed} emoji-only comments; wrote {kept} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())