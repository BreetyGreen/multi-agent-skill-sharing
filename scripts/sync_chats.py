#!/usr/bin/env python3
"""
sync_chats.py — v2: aggregate chat history across AI coding agents into one
neutral, git-friendly archive.

Companion to v1's distribute.py. Where v1 fans a *skill* out to every agent,
v2 pulls every agent's *chat history* into a single Canonical archive you can
read, search, back up, and commit alongside your repo.

READ-ONLY BY DESIGN. This tool never writes to any agent's storage. SQLite
databases (Cursor / Antigravity) are opened in immutable read-only mode.

Supported sources (auto-detected on this machine):
    claude       ~/.claude/projects/**/*.jsonl
    workbuddy    ~/.workbuddy/projects/**/*.jsonl
    codex        ~/.codex/sessions/**/*.jsonl (+ archived_sessions)
    cursor       ~/Library/Application Support/Cursor/.../globalStorage/state.vscdb
    antigravity  ~/Library/Application Support/Antigravity/.../state.vscdb

Usage
-----
    # Export everything available to ./chat-archive
    python3 sync_chats.py

    # Only some agents, custom output dir
    python3 sync_chats.py --agents claude,workbuddy,codex --out ./chat-archive

    # Preview what would be written, without touching disk
    python3 sync_chats.py --dry-run

    # Only sessions created on/after a date
    python3 sync_chats.py --since 2026-06-01

    # Also build a single-file offline HTML timeline
    python3 sync_chats.py --html

Cross-platform, pure standard library.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Type

# Allow running as a plain script (python3 sync_chats.py) OR as a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chatsync.base import Reader  # noqa: E402
from chatsync.canonical import CanonicalSession  # noqa: E402
from chatsync.exporter import export_sessions  # noqa: E402
from chatsync.readers.jsonl_reader import ClaudeReader, WorkBuddyReader  # noqa: E402
from chatsync.readers.codex_reader import CodexReader  # noqa: E402
from chatsync.readers.sqlite_reader import (  # noqa: E402
    AntigravityReader,
    CursorReader,
)

REGISTRY: Dict[str, Type[Reader]] = {
    "claude": ClaudeReader,
    "workbuddy": WorkBuddyReader,
    "codex": CodexReader,
    "cursor": CursorReader,
    "antigravity": AntigravityReader,
}
DEFAULT_AGENTS = list(REGISTRY.keys())


def _filter_since(sessions: List[CanonicalSession], since: str) -> List[CanonicalSession]:
    if not since:
        return sessions
    out = []
    for s in sessions:
        stamp = (s.created_at or s.updated_at or "")[:10]
        if stamp and stamp >= since:
            out.append(s)
    return out


def collect(agents: List[str], since: str = "") -> List[CanonicalSession]:
    all_sessions: List[CanonicalSession] = []
    for agent in agents:
        cls = REGISTRY.get(agent)
        if cls is None:
            print(f"  ! unknown agent '{agent}', skipping", file=sys.stderr)
            continue
        reader = cls()
        if not reader.available():
            print(f"  - {reader.display_name}: not found on this machine, skipping")
            continue
        sessions = reader.read_all()
        sessions = _filter_since(sessions, since)
        non_empty = [s for s in sessions if s.non_empty_messages()]
        print(
            f"  ✓ {reader.display_name}: {len(non_empty)} sessions "
            f"({len(sessions) - len(non_empty)} empty skipped)"
        )
        all_sessions.extend(sessions)
    return all_sessions


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="v2: aggregate AI agent chat histories into one archive (read-only)."
    )
    parser.add_argument(
        "--agents",
        default=",".join(DEFAULT_AGENTS),
        help="comma-separated: " + ",".join(DEFAULT_AGENTS),
    )
    parser.add_argument(
        "--out",
        default="./chat-archive",
        help="output directory for the archive (default: ./chat-archive)",
    )
    parser.add_argument(
        "--since", default="", help="only sessions on/after YYYY-MM-DD"
    )
    parser.add_argument(
        "--min-messages",
        type=int,
        default=1,
        help="skip sessions with fewer than N non-empty messages (default 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be exported without writing any files",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="also build a single-file offline HTML timeline (viewer.html)",
    )
    parser.add_argument(
        "--html-max",
        type=int,
        default=300,
        help="cap sessions embedded in the HTML viewer (0 = no cap; default 300). "
        "Full data always stays in the per-session JSON/MD files.",
    )
    args = parser.parse_args(argv)

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]

    print("chatsync v2 — read-only chat-history aggregation")
    print(f"  agents : {', '.join(agents)}")
    print(f"  out    : {os.path.abspath(args.out)}")
    if args.since:
        print(f"  since  : {args.since}")
    if args.dry_run:
        print("  MODE   : DRY RUN (no files will be written)")
    print()

    sessions = collect(agents, args.since)
    print()

    manifest = export_sessions(
        sessions,
        out_dir=args.out,
        dry_run=args.dry_run,
        min_messages=args.min_messages,
    )

    print("Summary")
    print(f"  exported sessions : {manifest['exported']}")
    print(f"  skipped (empty)   : {manifest['skipped_empty']}")
    for agent, n in sorted(manifest["counts_by_agent"].items()):
        print(f"    - {agent:12s}: {n}")

    if not args.dry_run:
        print(f"\n  archive written to: {os.path.abspath(args.out)}")
        print(f"  manifest         : {os.path.join(os.path.abspath(args.out), 'index.json')}")

    if args.html:
        try:
            from chatsync.html_viewer import build_html

            html_path = build_html(
                [s for s in sessions if s.non_empty_messages()],
                args.out,
                dry_run=args.dry_run,
                max_sessions=args.html_max,
            )
            if html_path:
                print(f"  HTML timeline    : {html_path}")
        except Exception as e:  # pragma: no cover
            print(f"  ! HTML build failed: {type(e).__name__}: {e}", file=sys.stderr)

    if not args.dry_run:
        print(
            "\nNote: this archive contains your private conversations. "
            "It is git-ignored by default (see .gitignore).\n"
            "      Review the contents before sharing or committing anywhere."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
