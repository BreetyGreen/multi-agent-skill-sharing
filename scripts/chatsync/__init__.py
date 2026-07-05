#!/usr/bin/env python3
"""chatsync — cross-product chat-history sync (v2).

See docs/V2_CHAT_SYNC_DESIGN.md for the full design. Read-only export of
chat histories from Claude Code, WorkBuddy, Codex CLI, Cursor and Antigravity
into a Canonical intermediate model, then Markdown / JSON / HTML.
"""

from .canonical import (  # noqa: F401
    Block,
    CanonicalMessage,
    CanonicalSession,
    SCHEMA_VERSION,
)
from .base import Reader  # noqa: F401

__all__ = [
    "Block",
    "CanonicalMessage",
    "CanonicalSession",
    "SCHEMA_VERSION",
    "Reader",
]
