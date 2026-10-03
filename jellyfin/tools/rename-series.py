#!/usr/bin/env python3
"""Rename video files as Jellyfin series episodes."""

import argparse
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename series episodes for Jellyfin")
    parser.add_argument(
        "--path", type=Path, required=True, help="Directory containing the videos"
    )
    parser.add_argument("--series-name", required=True, help="Series name")
    parser.add_argument(
        "--season", type=positive_int, default=1, help="Season number (default: 1)"
    )
    parser.add_argument(
        "--start",
        type=positive_int,
        default=1,
        help="First episode number (default: 1)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Rename files (default: preview only)"
    )
    args = parser.parse_args()

    if not args.path.is_dir():
        parser.error(f"cannot access directory: {args.path}")
    if not args.series_name.strip() or Path(args.series_name).name != args.series_name:
        parser.error("series name must be a valid file name")

    files = sorted(
        (
            f
            for f in args.path.iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        ),
        key=lambda f: f.name.casefold(),
    )
    changes = [
        (
            source,
            source.with_name(
                f"{args.series_name} s{args.season:02d}e{episode:02d}{source.suffix}"
            ),
        )
        for episode, source in enumerate(files, start=args.start)
    ]

    collisions = [
        target for source, target in changes if target != source and target.exists()
    ]
    if collisions:
        parser.error(
            "destination files already exist: " + ", ".join(str(p) for p in collisions)
        )

    for source, target in changes:
        print(f"{source.name} -> {target.name}")

    if not args.apply:
        print("Preview only: no files were changed. Use --apply to rename them.")
        return 0

    if changes:
        with (args.path / "rename.log").open("a", encoding="utf-8") as log:
            for source, target in changes:
                if source != target:
                    source.rename(target)
                    log.write(f"{source.name} -> {target.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
