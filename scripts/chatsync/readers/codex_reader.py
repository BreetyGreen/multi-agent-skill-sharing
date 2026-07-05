#!/usr/bin/env python3
"""
codex_reader.py — reader for Codex CLI rollout logs.

Storage:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
  ~/.codex/archived_sessions/...   (same shape)

Each line is an event: {timestamp(ISO), type, payload}. The relevant
payload.type values observed on-disk:

  session_meta        -> {id, cwd, timestamp, cli_version, ...}   (project + id)
  turn_context        -> per-turn model/policy metadata (skipped for text)
  task_started        -> turn boundary (skipped)
  message             -> {role: user|assistant|developer, content:[blocks]}
                         (early rollout format; content blocks input_text/...)
  user_message        -> {message: str, images, ...}   (later format)
  agent_message       -> {message: str, phase, ...}     (later format)
  reasoning           -> {summary, content, encrypted_content}
  function_call       -> {name, arguments, call_id}
  function_call_output-> {call_id, output}
  token_count         -> usage stats (folded into last assistant meta)
  task_complete       -> {last_agent_message, duration_ms, ...}

We replay events in file order (already timestamp-ordered) and fold tool
calls / reasoning into blocks on the surrounding assistant turn where it
reads naturally, while keeping user/assistant text as top-level messages.

De-duplication note
-------------------
Codex writes each real user turn TWICE: once as an early `message`
(role=user, content blocks) and again as a later `user_message` (flat
`message` string). Assistant turns similarly appear as `message`
(role=assistant) and/or `agent_message`. We therefore prefer the flat
`user_message` / `agent_message` events, and only fall back to `message`
events for content that wasn't already captured (deduped by normalized text).
System/developer scaffolding messages are dropped by default.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Set

from ..base import Reader
from ..canonical import (
    Block,
    BLOCK_REASONING,
    BLOCK_TOOL_RESULT,
    BLOCK_TOOL_USE,
    CanonicalMessage,
    CanonicalSession,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
)
from .. import utils


def _norm_key(text: str) -> str:
    """Whitespace-insensitive key for de-duplicating the same turn."""
    return " ".join((text or "").split())


class CodexReader(Reader):
    agent_id = "codex"
    display_name = "Codex CLI"
    roots = ["~/.codex/sessions", "~/.codex/archived_sessions"]

    def __init__(self, keep_system: bool = False) -> None:
        # scaffolding (developer/system + environment/permission blocks) is
        # dropped by default; flip keep_system=True to retain it.
        self.keep_system = keep_system

    def available(self) -> bool:
        return any(os.path.isdir(os.path.expanduser(r)) for r in self.roots)

    def _session_files(self) -> List[str]:
        files: List[str] = []
        for r in self.roots:
            base = os.path.expanduser(r)
            if os.path.isdir(base):
                files.extend(
                    glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
                )
        return sorted(files)

    def iter_sessions(self) -> Iterator[CanonicalSession]:
        for path in self._session_files():
            try:
                sess = self._parse_file(path)
            except Exception as e:
                sess = CanonicalSession(
                    session_id=os.path.splitext(os.path.basename(path))[0],
                    source_agent=self.agent_id,
                    source_ref=path,
                )
                sess.note_lossy(f"parse error: {type(e).__name__}: {e}")
            if sess is not None:
                yield sess

    # ------------------------------------------------------------------
    def _parse_file(self, path: str) -> Optional[CanonicalSession]:
        events: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not events:
            return None

        sess = CanonicalSession(
            session_id=os.path.splitext(os.path.basename(path))[0],
            source_agent=self.agent_id,
            source_ref=path,
        )
        sess.note_lossy(
            "Codex reasoning summaries and token stats kept as blocks/meta; "
            "encrypted_content dropped."
        )

        # Pre-scan: collect the normalized texts of the clean flat events
        # (user_message / agent_message) so we can suppress the duplicate
        # `message` events for the same turns.
        flat_user: Set[str] = set()
        flat_agent: Set[str] = set()
        for ev in events:
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if p.get("type") == "user_message":
                k = _norm_key(p.get("message") or utils.flatten_text(p.get("content")))
                if k:
                    flat_user.add(k)
            elif p.get("type") == "agent_message":
                k = _norm_key(p.get("message") or "")
                if k:
                    flat_agent.add(k)
        has_flat = bool(flat_user or flat_agent)

        first_ts: Optional[str] = None
        last_ts: Optional[str] = None
        seen_user: Set[str] = set()
        seen_agent: Set[str] = set()

        for ev in events:
            ts = utils.to_iso(ev.get("timestamp"))
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            ptype = payload.get("type") or ev.get("type")

            if ptype == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    sess.project = cwd
                cid = payload.get("id")
                if cid:
                    sess.session_id = str(cid)
                continue

            if ptype == "message":
                role = payload.get("role")
                text = utils.flatten_text(payload.get("content"))
                if not text.strip():
                    continue
                key = _norm_key(text)
                if role == "user":
                    # drop if a clean user_message already carries this turn,
                    # or if it's environment scaffolding
                    if has_flat and key in flat_user:
                        continue
                    if key in seen_user:
                        continue
                    if self._is_scaffolding(text):
                        if not self.keep_system:
                            continue
                        sess.messages.append(
                            CanonicalMessage(role="system", text=text, timestamp=ts)
                        )
                        continue
                    seen_user.add(key)
                    sess.messages.append(
                        CanonicalMessage(role=ROLE_USER, text=text, timestamp=ts)
                    )
                elif role == "assistant":
                    if has_flat and key in flat_agent:
                        continue
                    if key in seen_agent:
                        continue
                    if self._is_scaffolding(text):
                        continue
                    seen_agent.add(key)
                    sess.messages.append(
                        CanonicalMessage(role=ROLE_ASSISTANT, text=text, timestamp=ts)
                    )
                else:
                    # developer / system scaffolding
                    if self.keep_system and not self._is_scaffolding(text):
                        sess.messages.append(
                            CanonicalMessage(role="system", text=text, timestamp=ts)
                        )
                continue

            if ptype == "user_message":
                text = payload.get("message") or utils.flatten_text(
                    payload.get("content")
                )
                if isinstance(text, str) and text.strip():
                    if self._is_scaffolding(text) and not self.keep_system:
                        continue
                    key = _norm_key(text)
                    if key in seen_user:
                        continue
                    seen_user.add(key)
                    sess.messages.append(
                        CanonicalMessage(role=ROLE_USER, text=text, timestamp=ts)
                    )
                continue

            if ptype == "agent_message":
                text = payload.get("message") or ""
                if isinstance(text, str) and text.strip():
                    if self._is_scaffolding(text):
                        continue
                    key = _norm_key(text)
                    if key in seen_agent:
                        continue
                    seen_agent.add(key)
                    sess.messages.append(
                        CanonicalMessage(
                            role=ROLE_ASSISTANT, text=text, timestamp=ts
                        )
                    )
                continue

            if ptype == "reasoning":
                summary = payload.get("summary")
                content = payload.get("content")
                rtext = utils.flatten_text(summary) or utils.flatten_text(content)
                if rtext.strip():
                    sess.messages.append(
                        CanonicalMessage(
                            role=ROLE_ASSISTANT,
                            text="",
                            timestamp=ts,
                            blocks=[Block(kind=BLOCK_REASONING, text=rtext)],
                        )
                    )
                continue

            if ptype == "function_call":
                name = payload.get("name", "")
                args = payload.get("arguments")
                args_text = args if isinstance(args, str) else json.dumps(
                    args, ensure_ascii=False
                )
                blk = Block(
                    kind=BLOCK_TOOL_USE,
                    text=f"[tool_use] {name}({utils.truncate(args_text, 800)})",
                    raw={"name": name, "call_id": payload.get("call_id")},
                )
                sess.messages.append(
                    CanonicalMessage(
                        role=ROLE_ASSISTANT, text="", timestamp=ts, blocks=[blk]
                    )
                )
                continue

            if ptype == "function_call_output":
                out = payload.get("output")
                out_text = utils.flatten_text(out) or (
                    out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                )
                sess.messages.append(
                    CanonicalMessage(
                        role=ROLE_TOOL,
                        text="",
                        timestamp=ts,
                        blocks=[
                            Block(
                                kind=BLOCK_TOOL_RESULT,
                                text="[tool_result] "
                                + utils.truncate(out_text, 1500),
                                raw={"call_id": payload.get("call_id")},
                            )
                        ],
                    )
                )
                continue

            if ptype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and sess.messages:
                    sess.messages[-1].meta.setdefault("tokens", info)
                continue

            # task_started / task_complete / turn_context / unknown -> skip
            continue

        sess.created_at = first_ts
        sess.updated_at = last_ts
        if not sess.project:
            sess.project = utils.decode_project_dir(
                os.path.basename(os.path.dirname(path))
            )
        sess.title = sess.derive_title()
        return sess

    # ------------------------------------------------------------------
    @staticmethod
    def _is_scaffolding(text: str) -> bool:
        """True for Codex's injected context blocks (not human turns).

        Codex stuffs a lot of machine-generated material into `message`
        events with role user/assistant: environment/permission blocks,
        the project AGENTS.md, approval-decision JSON, and — importantly —
        *replayed history from prior sessions* ("The following is the Codex
        agent history…"), which would otherwise create cross-session dupes.
        """
        t = (text or "").lstrip()
        head = t[:80].lower()
        if head.startswith(
            (
                "<environment_context",
                "<permissions instructions",
                "<app-context",
                "<user_instructions",
                "# codex desktop context",
                "# agents.md",
                "the following is the codex agent history",
            )
        ):
            return True
        # approval-decision JSON emitted by the sandbox, not a real reply
        if t.startswith("{") and (
            '"outcome"' in head or '"risk_level"' in head
        ):
            return True
        return False
