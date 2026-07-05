#!/usr/bin/env python3
"""
base.py — Reader abstraction shared by every source adapter.

A Reader knows how to find one agent's sessions on disk and parse each into a
CanonicalSession. Readers must be:
  - read-only (NEVER mutate the source; SQLite opened with mode=ro)
  - resilient (a single malformed session must not crash the whole run)
  - honest (record lossy_notes for anything dropped)
"""

from __future__ import annotations

import abc
from typing import Iterator, List

from .canonical import CanonicalSession


class Reader(abc.ABC):
    #: stable agent id used in output paths and Canonical.source_agent
    agent_id: str = "base"
    #: human-friendly display name
    display_name: str = "Base"

    @abc.abstractmethod
    def available(self) -> bool:
        """True if this agent's storage exists on the current machine."""
        raise NotImplementedError

    @abc.abstractmethod
    def iter_sessions(self) -> Iterator[CanonicalSession]:
        """Yield each parsed session. Must swallow per-session errors and
        continue, appending a lossy_note where meaningful."""
        raise NotImplementedError

    def read_all(self) -> List[CanonicalSession]:
        out: List[CanonicalSession] = []
        for s in self.iter_sessions():
            if s is not None:
                out.append(s)
        return out
