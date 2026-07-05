#!/usr/bin/env python3
"""
exporter.py — render CanonicalSession objects to disk.

Two neutral, git-friendly artifacts per session:
  - Markdown (human-readable)
  - JSON     (machine-readable, full Canonical dump)

Archive layout:
  <out>/<agent>/<YYYY-MM-DD>/<slug>-<shortid>.md
  <out>/<agent>/<YYYY-MM-DD>/<slug>-<shortid>.json
plus a top-level index.json manifest of everything exported.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List

from .canonical import (
    BLOCK_REASONING,
    BLOCK_TOOL_RESULT,
    BLOCK_TOOL_USE,
    SCHEMA_VERSION,
    CanonicalSession,
)
from . import utils

ROLE_LABEL = {
    "user": "🧑 User",
    "assistant": "🤖 Assistant",
    "system": "⚙️ System",
    "tool": "🔧 Tool",
}


def session_relpath(sess: CanonicalSession) -> str:
    """Stable relative path (without extension) for a session's artifacts."""
    date = utils.date_part(sess.created_at or sess.updated_at)
    short = sess.session_id.replace(":", "_")[:8] or "noid"
    slug = utils.slugify(sess.title or sess.derive_title(), 40)
    return os.path.join(sess.source_agent, date, f"{slug}-{short}")


def render_markdown(sess: CanonicalSession) -> str:
    lines: List[str] = []
    title = sess.title or sess.derive_title()
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- **Agent**: {sess.source_agent}")
    if sess.project:
        lines.append(f"- **Project**: `{sess.project}`")
    lines.append(f"- **Session ID**: `{sess.session_id}`")
    if sess.created_at:
        lines.append(f"- **Created**: {sess.created_at}")
    if sess.updated_at:
        lines.append(f"- **Updated**: {sess.updated_at}")
    lines.append(f"- **Messages**: {len(sess.non_empty_messages())}")
    lines.append(f"- **Source**: `{sess.source_ref}`")
    if sess.lossy_notes:
        lines.append("")
        lines.append("> ⚠️ **Lossy conversion notes**:")
        for n in sess.lossy_notes:
            lines.append(f"> - {n}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for m in sess.messages:
        has_text = bool(m.text and m.text.strip())
        tool_blocks = [
            b
            for b in m.blocks
            if b.kind in (BLOCK_TOOL_USE, BLOCK_TOOL_RESULT, BLOCK_REASONING)
        ]
        if not has_text and not tool_blocks:
            continue
        label = ROLE_LABEL.get(m.role, m.role)
        ts = f"  ·  _{m.timestamp}_" if m.timestamp else ""
        lines.append(f"### {label}{ts}")
        lines.append("")
        if has_text:
            lines.append(m.text.rstrip())
            lines.append("")
        for b in tool_blocks:
            if b.kind == BLOCK_REASONING:
                lines.append("<details><summary>💭 reasoning</summary>")
                lines.append("")
                lines.append(utils.truncate(b.text, 3000))
                lines.append("")
                lines.append("</details>")
            else:
                lines.append("```text")
                lines.append(utils.truncate(b.text, 2000))
                lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def export_sessions(
    sessions: List[CanonicalSession],
    out_dir: str,
    dry_run: bool = False,
    min_messages: int = 1,
) -> Dict[str, object]:
    """Write MD+JSON for each session. Returns a manifest dict."""
    manifest: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "chatsync/sync_chats.py",
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds"),
        "out_dir": os.path.abspath(out_dir),
        "sessions": [],
    }
    counts: Dict[str, int] = {}
    exported = 0
    skipped_empty = 0

    for sess in sessions:
        ne = sess.non_empty_messages()
        if len(ne) < min_messages:
            skipped_empty += 1
            continue
        rel = session_relpath(sess)
        md_rel = rel + ".md"
        json_rel = rel + ".json"
        counts[sess.source_agent] = counts.get(sess.source_agent, 0) + 1
        exported += 1

        entry = {
            "agent": sess.source_agent,
            "title": sess.title or sess.derive_title(),
            "session_id": sess.session_id,
            "project": sess.project,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "messages": len(ne),
            "markdown": md_rel,
            "json": json_rel,
        }
        manifest["sessions"].append(entry)

        if dry_run:
            continue

        md_path = os.path.join(out_dir, md_rel)
        json_path = os.path.join(out_dir, json_rel)
        os.makedirs(os.path.dirname(md_path), exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(sess))
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.write(sess.to_json())

    manifest["exported"] = exported
    manifest["skipped_empty"] = skipped_empty
    manifest["counts_by_agent"] = counts

    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)

    return manifest
