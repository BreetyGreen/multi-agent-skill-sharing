#!/usr/bin/env python3
"""
utils.py — shared helpers for chatsync readers/exporters.

Pure standard library. Handles the messy parts every reader needs:
timestamp normalization (epoch ms / ISO / seconds), VSCode-style project
path decoding, safe filename slugging, and text flattening.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
import sys
from typing import Any, List, Optional


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def to_iso(ts: Any) -> Optional[str]:
    """Normalize a timestamp of unknown flavor to an ISO-8601 UTC string.

    Accepts:
      - int/float epoch seconds (< 1e12) or milliseconds (>= 1e12)
      - already-ISO strings (returned as-is after a light sanity check)
    Returns None if it can't be parsed.
    """
    if ts is None:
        return None
    # numeric epoch
    if isinstance(ts, (int, float)):
        val = float(ts)
        if val <= 0:
            return None
        # milliseconds if it's too big to be a plausible second-epoch
        if val >= 1e12:
            val /= 1000.0
        try:
            return (
                _dt.datetime.fromtimestamp(val, tz=_dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    # string
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # numeric-looking string
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return to_iso(float(s))
        # already ISO-ish
        return s
    return None


def iso_sort_key(iso: Optional[str]) -> str:
    """Sort key that pushes None/empty timestamps to the front deterministically."""
    return iso or ""


def date_part(iso: Optional[str]) -> str:
    """Extract YYYY-MM-DD from an ISO string, or 'unknown-date'."""
    if not iso:
        return "unknown-date"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", iso)
    return m.group(1) if m else "unknown-date"


# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------

def decode_project_dir(encoded: str) -> str:
    """Decode a Claude/WorkBuddy project directory name back to a real path.

    Claude Code encodes cwd like:  -Users-einar-CodeBuddy-ai-project-...
    (leading dash = leading slash; dashes = path separators). This is lossy
    because real dashes in path segments are indistinguishable from
    separators, so we only use it as a best-effort fallback label.
    """
    if not encoded:
        return ""
    s = encoded
    if s.startswith("-"):
        s = "/" + s[1:]
    return s.replace("-", "/")


def project_label(project: str) -> str:
    """A short human label for a project path (basename-ish)."""
    if not project:
        return "unknown-project"
    p = project.rstrip("/")
    return p.rsplit("/", 1)[-1] or p or "unknown-project"


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a filesystem-safe slug (keeps unicode word chars)."""
    if not text:
        return "untitled"
    s = text.strip().replace(" ", "-")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"


# ---------------------------------------------------------------------------
# Text flattening
# ---------------------------------------------------------------------------

def flatten_text(content: Any) -> str:
    """Best-effort extract human-readable text from a variety of content shapes.

    Handles:
      - plain string
      - list of blocks: {"type": "text"|"input_text"|"output_text", "text": ...}
        or {"type": "tool_result", "content": ...}
      - dict with a 'text' field
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return flatten_text(content["content"])
        return ""
    if isinstance(content, list):
        parts: List[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t in ("text", "input_text", "output_text") and isinstance(
                    b.get("text"), str
                ):
                    parts.append(b["text"])
                elif isinstance(b.get("text"), str):
                    parts.append(b["text"])
        return "\n".join(p for p in parts if p)
    return ""


def truncate(text: str, limit: int = 4000) -> str:
    """Truncate long tool outputs for readability, marking the cut."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


# ---------------------------------------------------------------------------
# Clipboard (best-effort, cross-platform, stdlib only)
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """Copy `text` to the OS clipboard. Returns True on success.

    Best-effort and dependency-free — shells out to whichever native tool is
    present. Never raises; returns False if no clipboard tool is available.
      - macOS:   pbcopy
      - Windows: clip
      - Linux:   wl-copy (Wayland) / xclip / xsel (X11)
    """
    if text is None:
        return False
    data = text.encode("utf-8")

    def _try(cmd: List[str]) -> bool:
        if not shutil.which(cmd[0]):
            return False
        try:
            p = subprocess.run(cmd, input=data, check=True)
            return p.returncode == 0
        except Exception:
            return False

    plat = sys.platform
    if plat == "darwin":
        return _try(["pbcopy"])
    if plat.startswith("win"):
        # `clip` expects the text on stdin; it's always present on Windows.
        try:
            subprocess.run(["clip"], input=data, check=True)
            return True
        except Exception:
            return False
    # Linux / other unix: try Wayland then X11 helpers.
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if _try(cmd):
            return True
    return False
