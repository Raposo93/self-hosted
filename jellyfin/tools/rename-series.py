#!/usr/bin/env python3
"""Rename video files as Jellyfin series episodes."""

import argparse
import json
import os
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}
LOG_NAME = "rename.log"


def exists(path: Path) -> bool:
    """Include broken symlinks when checking for filename conflicts."""
    return os.path.lexists(path)


def undo(path: Path, parser: argparse.ArgumentParser) -> int:
    log_path = path / LOG_NAME
    if not log_path.is_file():
        parser.error(f"no rename log found: {log_path}")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[-1] == "# undone":
        parser.error("no applied operation to undo")
    if lines[-1] == "# applied":
        parser.error(
            "last operation has no file timestamps; undo it manually from rename.log"
        )
    if not lines[-1].startswith("# applied "):
        parser.error("rename log has no complete latest operation to undo")

    starts = [line for line in lines if line.startswith("# apply ")]
    if not starts:
        parser.error(
            "rename log has no operation boundaries; older renames cannot be undone safely"
        )
    try:
        changes = json.loads(lines[-1][len("# applied ") :])
        if not isinstance(changes, list) or not changes:
            raise ValueError("empty or invalid operation")
        for change in changes:
            if (
                not isinstance(change, dict)
                or set(change)
                != {"source", "target", "dev", "ino", "size", "mtime_ns", "ctime_ns"}
                or any(
                    not isinstance(change[key], str)
                    or change[key] in {".", ".."}
                    or Path(change[key]).name != change[key]
                    for key in ("source", "target")
                )
                or not all(
                    isinstance(change[key], int)
                    for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
                )
            ):
                raise ValueError("invalid rename entry")
        started = json.loads(starts[-1][len("# apply ") :])
        if started != [
            {"source": change["source"], "target": change["target"]}
            for change in changes
        ]:
            raise ValueError("operation boundaries do not match")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        parser.error(f"invalid rename log: {exc}")
    if (
        len({change["source"] for change in changes}) != len(changes)
        or len({change["target"] for change in changes}) != len(changes)
        or any(change["source"] == change["target"] for change in changes)
    ):
        parser.error("invalid rename log: duplicate or unchanged names")

    problems = []
    for change in changes:
        source = path / change["source"]
        target = path / change["target"]
        if exists(source):
            problems.append(f"original name already exists: {source}")
        try:
            stat = target.lstat()
        except FileNotFoundError:
            problems.append(f"renamed file is missing: {target}")
        else:
            if (
                stat.st_dev != change["dev"]
                or stat.st_ino != change["ino"]
                or stat.st_size != change["size"]
                or stat.st_mtime_ns != change["mtime_ns"]
                or stat.st_ctime_ns != change["ctime_ns"]
            ):
                problems.append(f"renamed file has changed: {target}")
    if problems:
        parser.error("cannot undo:\n  " + "\n  ".join(problems))

    with log_path.open("a", encoding="utf-8") as log:
        for change in reversed(changes):
            source = path / change["source"]
            target = path / change["target"]
            target.rename(source)
            print(f"{target.name} -> {source.name}")
        log.write("# undone\n")
    return 0


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename series episodes for Jellyfin")
    parser.add_argument(
        "directory", type=Path, nargs="?", help="Directory containing the videos"
    )
    parser.add_argument("series", nargs="?", help="Series name")
    parser.add_argument("--path", type=Path, help="Directory containing the videos")
    parser.add_argument("--series-name", help="Series name")
    parser.add_argument(
        "--season", type=positive_int, default=1, help="Season number (default: 1)"
    )
    parser.add_argument(
        "--start",
        type=positive_int,
        default=1,
        help="First episode number (default: 1)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="Rename files (default: preview only)"
    )
    mode.add_argument(
        "--undo", action="store_true", help="Undo the last applied rename"
    )
    args = parser.parse_args()

    if args.directory is not None and args.path is not None:
        parser.error("specify the directory either positionally or with --path")
    if args.series is not None and args.series_name is not None:
        parser.error("specify the series either positionally or with --series-name")
    path = args.path or args.directory
    series_name = args.series_name or args.series
    if path is None or not path.is_dir():
        parser.error(f"cannot access directory: {path}")
    if args.undo:
        if series_name is not None:
            parser.error("--undo does not take a series name")
        return undo(path, parser)
    if (
        series_name is None
        or not series_name.strip()
        or Path(series_name).name != series_name
    ):
        parser.error("series name must be a valid file name")

    files = sorted(
        (f for f in path.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS),
        key=lambda f: f.name.casefold(),
    )
    changes = [
        (
            source,
            source.with_name(
                f"{series_name} s{args.season:02d}e{episode:02d}{source.suffix}"
            ),
        )
        for episode, source in enumerate(files, start=args.start)
    ]

    collisions = [
        target for source, target in changes if target != source and exists(target)
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

    changed = [(source, target) for source, target in changes if source != target]
    if changed:
        log_path = path / LOG_NAME
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            if any(line.startswith("# apply ") for line in lines) and not (
                lines[-1] in {"# applied", "# undone"}
                or lines[-1].startswith("# applied ")
            ):
                parser.error(
                    "rename log contains an incomplete operation; inspect it before applying again"
                )
        record = [
            {"source": source.name, "target": target.name} for source, target in changed
        ]
        with log_path.open("a", encoding="utf-8") as log:
            log.write("# apply " + json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            for source, target in changed:
                source.rename(target)
                log.write(f"{source.name} -> {target.name}\n")
                log.flush()
            completed = []
            for source, target in changed:
                stat = target.lstat()
                completed.append(
                    {
                        "source": source.name,
                        "target": target.name,
                        "dev": stat.st_dev,
                        "ino": stat.st_ino,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "ctime_ns": stat.st_ctime_ns,
                    }
                )
            log.write("# applied " + json.dumps(completed, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
