"""Marker protocol parser.

Per ADR-0006, the marker format is:

    【<opener>】<content>【/<closer>】

with full-width corner brackets. The parser is **streaming**:
callers feed new bytes in via :meth:`MarkerParser.feed` and
receive a list of :class:`MarkerEvent` for each complete marker
encountered.

Ticket 02 only exercises the ``【进度汇报】`` opener. The
opener/closer registry is data-driven so later tickets (04, 05)
plug in :class:`MarkerKind.TO` / :class:`MarkerKind.REQUEST_EXEC`
/ :class:`MarkerKind.HELP` without parser changes.

Edge cases follow ADR-0006 §"边界条件" and ADR-0010:

- Body exceeds 64 KiB → truncate + emit :class:`MarkerEvent`
  with ``truncated=True``.
- Partial buffer exceeds 64 KiB without a closer → drop the
  buffer, emit a parse-warning :class:`MarkerEvent` with
  ``kind=MarkerKind.PARSE_WARNING``.
- Nested openers in body are ignored (the outer wins; the inner
  is treated as content). Ticket 05 verifies this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Final


class MarkerKind(str, Enum):
    """All known marker kinds. ``str`` mixin so they JSON-serialize."""

    PROGRESS = "progress"
    TO = "to"
    REQUEST_EXEC = "request_exec"
    HELP = "help"
    PARSE_WARNING = "parse_warning"


# (opener-text, closer-text, kind). Order doesn't affect matching;
# the parser scans for any opener and matches the corresponding
# closer. Registry is immutable at runtime.
_OPENER_REGISTRY: Final[tuple[tuple[str, str, MarkerKind], ...]] = (
    ("【进度汇报】", "【/进度汇报】", MarkerKind.PROGRESS),
    ("【申请执行】", "【/申请执行】", MarkerKind.REQUEST_EXEC),
    ("【求助人类】", "【/求助人类】", MarkerKind.HELP),
)


# TO: has a dynamic closer; handled separately, not via registry.
_TO_OPENER_RE = re.compile(r"【TO:([A-Za-z0-9_\-]+)】")
_TO_CLOSER_TEMPLATE = "【/TO:{name}】"


# Per-marker body size cap (ADR-0006 §"64 KiB 上限").
MAX_MARKER_BYTES: Final = 64 * 1024
# Partial-buffer cap; once the buffer grows past this with no
# closer in sight, we drop it and emit a parse-warning.
MAX_PARTIAL_BUFFER_BYTES: Final = 64 * 1024


@dataclass
class MarkerEvent:
    """One parsed marker (or one parse-warning)."""

    kind: MarkerKind
    content: str
    # For TO markers: the parsed target agent name. None otherwise.
    target: str | None = None
    # True if the body was truncated to MAX_MARKER_BYTES.
    truncated: bool = False
    # True if this event represents a parse-warning rather than
    # a real marker.
    warning: bool = False
    # The opener text actually seen. Useful for parse-warnings
    # that mention what was malformed.
    detail: str | None = None


class MarkerParser:
    """Stateful, single-agent marker parser.

    Not thread-safe; one parser per agent. The worker constructs
    one parser per ``state.agents[name]`` and feeds it the
    worker's per-agent incremental stdout.
    """

    __slots__ = ("_buffer", "_open", "_to_target")

    def __init__(self) -> None:
        self._buffer: str = ""
        # If a marker is currently being scanned, the opener text
        # and the closer we expect. None when between markers.
        self._open: tuple[str, str, MarkerKind] | None = None
        # For TO: we need to remember the target name across the
        # open/close pair.
        self._to_target: str | None = None

    def feed(self, chunk: str) -> list[MarkerEvent]:
        """Append ``chunk`` to the partial buffer and return any
        events that have been finalized by this batch.
        """
        events: list[MarkerEvent] = []
        self._buffer += chunk

        # Normal path: drain as many complete markers as possible.
        while True:
            ev = self._try_finalize_open() or self._try_open_new()
            if ev is None:
                break
            events.append(ev)

        # After draining, the remaining buffer must be bounded.
        # The cap is for *unclosed* partial state; a closed+parsed
        # marker doesn't count (it has already been advanced past).
        if self._open is not None and self.buffer_bytes > MAX_PARTIAL_BUFFER_BYTES:
            opener, _, _ = self._open
            events.append(
                MarkerEvent(
                    kind=MarkerKind.PARSE_WARNING,
                    content="",
                    warning=True,
                    detail=(
                        f"partial_buffer exceeded "
                        f"{MAX_PARTIAL_BUFFER_BYTES} bytes "
                        f"without a closer for {opener!r}"
                    ),
                )
            )
            self._open = None
            self._to_target = None
            self._buffer = ""
        return events

    # ----- internal: scan one step -----

    def _try_open_new(self) -> MarkerEvent | None:
        """If no marker is open, look for the next opener; if found,
        start tracking and recurse once to attempt completion in
        the same buffer (handles the case where the whole marker
        is already in the buffer)."""
        if self._open is not None:
            return self._try_finalize_open()

        # 1) Look for a TO: opener first (dynamic closer).
        m = _TO_OPENER_RE.search(self._buffer)
        if m is not None:
            target = m.group(1)
            opener_end = m.end()
            closer = _TO_CLOSER_TEMPLATE.format(name=target)
            closer_idx = self._buffer.find(closer, opener_end)
            if closer_idx == -1:
                # Don't open yet — wait for the closer to arrive.
                # The buffer is bounded by MAX_PARTIAL_BUFFER_BYTES
                # in feed() so this can't grow forever.
                return None
            content = self._buffer[opener_end:closer_idx]
            truncated = False
            if len(content.encode("utf-8")) > MAX_MARKER_BYTES:
                content = content.encode("utf-8")[:MAX_MARKER_BYTES].decode(
                    "utf-8", errors="replace"
                )
                truncated = True
            self._buffer = self._buffer[closer_idx + len(closer) :]
            return MarkerEvent(
                kind=MarkerKind.TO,
                content=content,
                target=target,
                truncated=truncated,
            )

        # 2) Walk the registry; pick the earliest opener match.
        earliest: tuple[int, tuple[str, str, MarkerKind]] | None = None
        for opener, closer, kind in _OPENER_REGISTRY:
            idx = self._buffer.find(opener)
            if idx == -1:
                continue
            if earliest is None or idx < earliest[0]:
                earliest = (idx, (opener, closer, kind))
        if earliest is None:
            return None

        idx, (opener, closer, kind) = earliest
        opener_end = idx + len(opener)
        closer_idx = self._buffer.find(closer, opener_end)
        if closer_idx == -1:
            # Defer — wait for the closer to arrive in a future
            # feed(). Mark which opener we're tracking so a later
            # call knows what to look for.
            self._open = (opener, closer, kind)
            return None
        content = self._buffer[opener_end:closer_idx]
        truncated = False
        if len(content.encode("utf-8")) > MAX_MARKER_BYTES:
            content = content.encode("utf-8")[:MAX_MARKER_BYTES].decode(
                "utf-8", errors="replace"
            )
            truncated = True
        self._buffer = self._buffer[closer_idx + len(closer) :]
        return MarkerEvent(
            kind=kind, content=content, truncated=truncated
        )

    def _try_finalize_open(self) -> MarkerEvent | None:
        """If a marker is open, look for its closer; if found,
        return the event and clear the open state."""
        if self._open is None:
            return None
        opener, closer, kind = self._open
        opener_idx = self._buffer.find(opener)
        if opener_idx == -1:
            # The opener we were tracking has been forgotten (e.g.
            # because of how we advanced the buffer previously).
            # Re-open using the same opener text from the start.
            self._open = None
            self._to_target = None
            return None
        opener_end = opener_idx + len(opener)
        closer_idx = self._buffer.find(closer, opener_end)
        if closer_idx == -1:
            return None
        content = self._buffer[opener_end:closer_idx]
        truncated = False
        if len(content.encode("utf-8")) > MAX_MARKER_BYTES:
            content = content.encode("utf-8")[:MAX_MARKER_BYTES].decode(
                "utf-8", errors="replace"
            )
            truncated = True
        self._buffer = self._buffer[closer_idx + len(closer) :]
        self._open = None
        self._to_target = None
        return MarkerEvent(
            kind=kind, content=content, truncated=truncated
        )

    # ----- introspection for tests -----

    @property
    def buffer_bytes(self) -> int:
        """Current partial buffer size in bytes (UTF-8)."""
        return len(self._buffer.encode("utf-8"))

    @property
    def is_open(self) -> bool:
        return self._open is not None


__all__ = [
    "MAX_MARKER_BYTES",
    "MAX_PARTIAL_BUFFER_BYTES",
    "MarkerEvent",
    "MarkerKind",
    "MarkerParser",
]


def all_marker_kinds() -> Iterable[MarkerKind]:
    """Public enumeration of known kinds (for dispatchers)."""
    return (
        MarkerKind.PROGRESS,
        MarkerKind.TO,
        MarkerKind.REQUEST_EXEC,
        MarkerKind.HELP,
    )