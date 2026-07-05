#!/usr/bin/env python3
"""
canonical.py — the Canonical intermediate model for chat-history sync (v2).

Every agent's native format (Claude JSONL, WorkBuddy JSONL, Codex event
stream, Cursor / Antigravity SQLite) is parsed by a Reader into these
dataclasses. Exporters then render Canonical -> Markdown / JSON / HTML.

Design principles (see docs/V2_CHAT_SYNC_DESIGN.md):
  - `text` is the least-common-denominator: human-readable, full-text
    searchable, always fillable by every source.
  - `blocks` preserves structured detail (tool calls, reasoning) when present.
  - `lossy_notes` HONESTLY records what a conversion dropped. We never pretend
    a transform was lossless.
  - `source_ref` keeps a pointer back to the original data for traceability.

Pure standard library. No third-party deps.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"

# Canonical roles. Anything a source can't map cleanly falls back to these.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLE_TOOL = "tool"
VALID_ROLES = {ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_TOOL}

# Canonical block kinds.
BLOCK_TEXT = "text"
BLOCK_TOOL_USE = "tool_use"
BLOCK_TOOL_RESULT = "tool_result"
BLOCK_REASONING = "reasoning"
BLOCK_IMAGE = "image"


@dataclass
class Block:
    """A structured sub-part of one message.

    kind: one of BLOCK_* above.
    text: human-readable rendering of this block (may be empty for pure tool IO).
    raw:  the source-specific structured payload, kept for high-fidelity needs.
    """

    kind: str
    text: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind}
        if self.text:
            d["text"] = self.text
        if self.raw:
            d["raw"] = self.raw
        return d


@dataclass
class CanonicalMessage:
    role: str
    text: str = ""
    timestamp: Optional[str] = None  # ISO-8601
    blocks: List[Block] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)  # model, tokens, ...

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role}
        if self.timestamp:
            d["timestamp"] = self.timestamp
        d["text"] = self.text
        if self.blocks:
            d["blocks"] = [b.to_dict() for b in self.blocks]
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class CanonicalSession:
    session_id: str
    source_agent: str  # claude|workbuddy|codex|cursor|antigravity
    source_ref: str = ""  # original file path or db key, for traceability
    project: str = ""  # abs path to the project this session belongs to
    created_at: Optional[str] = None  # ISO-8601
    updated_at: Optional[str] = None  # ISO-8601
    title: str = ""
    messages: List[CanonicalMessage] = field(default_factory=list)
    lossy_notes: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    # ---- convenience ----------------------------------------------------

    def note_lossy(self, msg: str) -> None:
        if msg and msg not in self.lossy_notes:
            self.lossy_notes.append(msg)

    def non_empty_messages(self) -> List[CanonicalMessage]:
        return [m for m in self.messages if (m.text or m.blocks)]

    def derive_title(self, max_len: int = 60) -> str:
        """Title = first *real* user line, skipping system-injected noise.

        Agents often inject system reminders / command wrappers as the first
        "user" turn (e.g. <system-reminder>, <command-message>, <user-context>).
        Those make useless titles, so we skip them and hunt for the first line
        that looks like something a human actually typed.
        """
        for m in self.messages:
            if m.role != ROLE_USER or not m.text.strip():
                continue
            line = _first_human_line(m.text)
            if line:
                if len(line) > max_len:
                    line = line[: max_len - 1].rstrip() + "…"
                return line
        return "(untitled session)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_agent": self.source_agent,
            "source_ref": self.source_ref,
            "project": self.project,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title or self.derive_title(),
            "messages": [m.to_dict() for m in self.messages],
            "lossy_notes": self.lossy_notes,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class CanonicalJSONEncoder(json.JSONEncoder):
    """Lets `json.dumps` handle the dataclasses directly if ever needed."""

    def default(self, o: Any) -> Any:
        if hasattr(o, "to_dict"):
            return o.to_dict()
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


# Lines/wrappers that are injected context, not something the user typed.
_NOISE_PREFIXES = (
    "<system-reminder",
    "<command-message",
    "<command-name",
    "<command-args",
    "<user-context",
    "<additional_data",
    "<local-command",
    "<env",
    "<environment_context",
    "<cwd",
    "</cwd",
    "<permissions instructions",
    "<app-context",
    "<user_instructions",
    "caveat:",
    "<user-prompt-submit-hook",
)
# Whole-tag lines we skip (open or close), e.g. "<user_query>".
_TAG_LINE_RE = re.compile(r"^</?[a-zA-Z0-9_\-]+[^>]*>$")


def _first_human_line(text: str) -> str:
    """Return the first line of `text` that looks human-authored.

    Strips XML-ish wrapper tags and known injected-context blocks. If the text
    contains a <user_query>…</user_query> block, prefer its inner content.
    """
    if not text:
        return ""
    # Prefer an explicit user query block if present.
    m = re.search(r"<user_query>\s*(.+?)\s*</user_query>", text, re.DOTALL)
    if m:
        inner = m.group(1).strip()
        for ln in inner.splitlines():
            ln = ln.strip()
            if ln:
                return ln
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        low = ln.lower()
        if any(low.startswith(p) for p in _NOISE_PREFIXES):
            continue
        if _TAG_LINE_RE.match(ln):
            continue
        # strip leading markdown heading / list / quote markers for a clean title
        ln = re.sub(r"^[#>\-\*\+\s]+", "", ln).strip()
        if not ln:
            continue
        return ln
    return ""


# Multi-line injected blocks we strip wholesale from a user message before
# using it as real conversational content (for handoff / summaries). Each entry
# is (open_tag_prefix, close_tag) — everything between is dropped.
_BLOCK_STRIP_TAGS = (
    ("<system-reminder", "</system-reminder>"),
    ("<additional_data", "</additional_data>"),
    ("<environment_context", "</environment_context>"),
    ("<identity_context", "</identity_context>"),
    ("<project_context", "</project_context>"),
    ("<command-message", "</command-message>"),
    ("<local-command-stdout", "</local-command-stdout>"),
    # Codex-specific runtime injections that wrap non-human turns:
    ("<turn_aborted", "</turn_aborted>"),
    ("<codex_internal_context", "</codex_internal_context>"),
    ("<skill", "</skill>"),
    ("<user_instructions", "</user_instructions>"),
)


# Whole user-messages that are actually agent/runtime-injected turns, not
# something the human typed. Codex in particular injects several of these each
# time a turn is resumed / a skill is loaded / a goal persists. If a user
# message *starts with* one of these signatures, we treat the whole message as
# non-human and drop it from handoff/summary content.
_INJECTED_TURN_SIGNATURES = (
    "the user interrupted the previous turn",
    "continue working toward the active thread goal",
    "the objective below is user-provided data",
    "continuation behavior:",
    "work from evidence:",
    "this goal persists across turns",
    "<name>",  # skill-invocation wrapper
    "the assistant has access to the following skills",
    "the following is the codex agent history",
)


def _is_injected_turn(text: str) -> bool:
    """True if a whole user message is a runtime-injected turn, not human input.

    Injections may be wrapped in an XML-ish tag (Codex wraps them in
    <turn_aborted>, <codex_internal_context …>, <skill>, etc.), so we peel any
    leading open-tags before matching the known signature sentences.
    """
    if not text:
        return False
    stripped = text.strip()
    # Peel up to a few leading open-tags (with optional attributes).
    for _ in range(4):
        m = re.match(r"^<[a-zA-Z_][\w\-]*(?:\s[^>]*)?>\s*", stripped)
        if not m:
            break
        stripped = stripped[m.end():].lstrip()
    head = stripped.lower()
    if any(head.startswith(sig) for sig in _INJECTED_TURN_SIGNATURES):
        return True
    # skill-load injections look like: [$name:name](/…/skills/…/SKILL.md)
    if re.match(r"^\[\$?[\w:\-]+\]\([^)]*/skills?/[^)]*\)", stripped):
        return True
    return False


def human_content(text: str) -> str:
    """Extract the *human-authored* body from a user message.

    Unlike `_first_human_line` (which returns just one line for a title), this
    returns the full user-typed content with system-injected scaffolding
    removed. Used by handoff so the carried-over conversation contains what the
    human actually said, not <system-reminder> / <additional_data> noise.

    Strategy:
      0. If the whole message is a runtime-injected turn (Codex "continue…",
         skill-load wrappers, etc.), return "" — it isn't human input.
      1. If an explicit <user_query>…</user_query> block exists, return the
         concatenation of ALL such blocks (a turn can carry more than one).
      2. Otherwise, strip known multi-line injected blocks, then drop leftover
         single-line noise (tag-only lines, known noise prefixes).
    """
    if not text:
        return ""
    # 0) Drop wholly-injected runtime turns.
    if _is_injected_turn(text):
        return ""
    # 1) Prefer explicit user_query blocks — that's exactly the human input.
    queries = re.findall(r"<user_query>\s*(.+?)\s*</user_query>", text, re.DOTALL)
    if queries:
        return "\n\n".join(q.strip() for q in queries if q.strip()).strip()

    # 2) Strip multi-line injected blocks (case-insensitive open tag match).
    cleaned = text
    for open_prefix, close_tag in _BLOCK_STRIP_TAGS:
        pattern = re.compile(
            re.escape(open_prefix) + r"[^>]*>.*?" + re.escape(close_tag),
            re.DOTALL | re.IGNORECASE,
        )
        cleaned = pattern.sub("", cleaned)

    # 3) Line-level cleanup of any leftover single-line noise.
    out: List[str] = []
    for raw in cleaned.splitlines():
        ln = raw.rstrip()
        low = ln.strip().lower()
        if not low:
            out.append("")  # keep paragraph breaks
            continue
        if any(low.startswith(p) for p in _NOISE_PREFIXES):
            continue
        if _TAG_LINE_RE.match(ln.strip()):
            continue
        out.append(ln)
    # collapse 3+ blank lines to a single blank line
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result
