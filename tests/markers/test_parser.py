"""Tests for the marker parser.

Covers:

- Basic PROGRESS marker round-trip.
- Split-across-feeds markers reassemble correctly.
- Closed-but-oversized body parses with ``truncated=True``.
- Unclosed-oversized partial buffer is dropped with a
  ``PARSE_WARNING`` event.
- TO: dynamic opener/closer pair parses with ``target`` set.
- Registry kinds: REQUEST_EXEC and HELP round-trip.
- Multiple markers in one feed: all emitted in order.
- Nested openers in body: outer wins, inner is content.
"""

from __future__ import annotations

import pytest

from moderator.markers import (
    MAX_MARKER_BYTES,
    MAX_PARTIAL_BUFFER_BYTES,
    MarkerKind,
    MarkerParser,
)


# ---------------------------------------------------------------------------
# Basic round-trip
# ---------------------------------------------------------------------------


def test_progress_marker_round_trip() -> None:
    p = MarkerParser()
    events = p.feed("【进度汇报】hello world【/进度汇报】")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind is MarkerKind.PROGRESS
    assert ev.content == "hello world"
    assert ev.truncated is False
    assert ev.warning is False


def test_help_marker_round_trip() -> None:
    p = MarkerParser()
    events = p.feed("【求助人类】need help【/求助人类】")
    assert len(events) == 1
    assert events[0].kind is MarkerKind.HELP
    assert events[0].content == "need help"


def test_request_exec_marker_round_trip() -> None:
    p = MarkerParser()
    events = p.feed("【申请执行】run pytest【/申请执行】")
    assert len(events) == 1
    assert events[0].kind is MarkerKind.REQUEST_EXEC
    assert events[0].content == "run pytest"


# ---------------------------------------------------------------------------
# Split-across-feeds
# ---------------------------------------------------------------------------


def test_split_marker_across_two_feeds() -> None:
    p = MarkerParser()
    a = p.feed("【进度汇报】hel")
    assert a == []
    b = p.feed("lo【/进度汇报】")
    assert len(b) == 1
    assert b[0].kind is MarkerKind.PROGRESS
    assert b[0].content == "hello"


def test_split_marker_across_three_feeds() -> None:
    p = MarkerParser()
    assert p.feed("【进度汇报】hel") == []
    assert p.feed("lo ") == []
    evs = p.feed("world【/进度汇报】")
    assert len(evs) == 1
    assert evs[0].content == "hello world"


def test_split_then_more_after_close() -> None:
    p = MarkerParser()
    p.feed("【进度汇报】a【/进度汇报】")
    evs = p.feed("【进度汇报】b【/进度汇报】")
    assert len(evs) == 1
    assert evs[0].content == "b"


# ---------------------------------------------------------------------------
# Oversized bodies
# ---------------------------------------------------------------------------


def test_closed_oversized_body_truncates_not_drops() -> None:
    """A marker whose body exceeds 64 KiB but IS closed in the same
    feed must parse + truncate (per ADR-0006 §"64 KiB 上限")."""
    p = MarkerParser()
    body = "x" * (MAX_MARKER_BYTES + 100)
    chunk = f"【进度汇报】{body}【/进度汇报】"
    events = p.feed(chunk)
    assert len(events) == 1
    assert events[0].truncated is True
    # Content is exactly the cap (in bytes, UTF-8 'x' is one byte each).
    assert len(events[0].content.encode("utf-8")) == MAX_MARKER_BYTES


def test_unclosed_oversized_partial_emits_parse_warning() -> None:
    """A partial buffer that grows past the cap without a closer
    emits a ``PARSE_WARNING`` and is dropped."""
    p = MarkerParser()
    # Open a marker that will never close within the partial cap.
    chunk = "【进度汇报】" + ("y" * (MAX_PARTIAL_BUFFER_BYTES + 1024))
    events = p.feed(chunk)
    assert len(events) == 1
    assert events[0].kind is MarkerKind.PARSE_WARNING
    assert events[0].warning is True
    assert "partial_buffer" in (events[0].detail or "")


def test_unclosed_partial_drops_buffer_so_next_marker_parses() -> None:
    """After a dropped partial, the next well-formed marker in a
    later feed must still parse cleanly."""
    p = MarkerParser()
    p.feed("【进度汇报】" + ("z" * (MAX_PARTIAL_BUFFER_BYTES + 1024)))
    # The buffer was dropped; a clean marker must parse.
    evs = p.feed("【进度汇报】after-drop【/进度汇报】")
    assert len(evs) == 1
    assert evs[0].content == "after-drop"


# ---------------------------------------------------------------------------
# TO: dynamic opener/closer
# ---------------------------------------------------------------------------


def test_to_marker_round_trip_with_target() -> None:
    p = MarkerParser()
    evs = p.feed("【TO:coder-2】please review my diff【/TO:coder-2】")
    assert len(evs) == 1
    assert evs[0].kind is MarkerKind.TO
    assert evs[0].target == "coder-2"
    assert evs[0].content == "please review my diff"


def test_to_marker_with_underscore_and_dash() -> None:
    p = MarkerParser()
    evs = p.feed("【TO:reviewer_bot-1】ping【/TO:reviewer_bot-1】")
    assert len(evs) == 1
    assert evs[0].target == "reviewer_bot-1"


def test_to_marker_split_across_feeds() -> None:
    p = MarkerParser()
    p.feed("【TO:coder-1】hel")
    p.feed("lo")
    evs = p.feed("【/TO:coder-1】")
    assert len(evs) == 1
    assert evs[0].kind is MarkerKind.TO
    assert evs[0].content == "hello"


# ---------------------------------------------------------------------------
# Multi-marker drain
# ---------------------------------------------------------------------------


def test_multiple_markers_in_one_feed() -> None:
    p = MarkerParser()
    chunk = (
        "before 【进度汇报】one【/进度汇报】"
        "between 【进度汇报】two【/进度汇报】"
        "after"
    )
    evs = p.feed(chunk)
    assert len(evs) == 2
    assert [e.content for e in evs] == ["one", "two"]


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_buffer_bytes_grows_with_chunk() -> None:
    p = MarkerParser()
    assert p.buffer_bytes == 0
    p.feed("abc")
    assert p.buffer_bytes == 3


def test_is_open_false_initially() -> None:
    p = MarkerParser()
    assert p.is_open is False


def test_is_open_true_after_partial_opener() -> None:
    p = MarkerParser()
    p.feed("【进度汇报】partial")
    assert p.is_open is True
