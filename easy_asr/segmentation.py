from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from easy_asr.engines.base import Segment


SENTENCE_END_RE = re.compile(r"[。.!！？!?；;]+[”’\"')）】]*$")
SENTENCE_SPLIT_RE = re.compile(r"([^。.!！？!?；;\n]+[。.!！？!?；;]*[”’\"')）】]*)")


@dataclass
class TimedWord:
    text: str
    start: float | None
    end: float | None
    raw: dict


def sentence_segments_from_words(
    index_start: int,
    segment_start: float,
    segment_end: float,
    words: list[Any],
    fallback_text: str,
    raw: dict,
    max_chars: int = 120,
) -> list[Segment]:
    timed_words = [_coerce_word(word) for word in words]
    timed_words = [word for word in timed_words if word.text.strip()]
    if not timed_words:
        return split_text_segment(index_start, segment_start, segment_end, fallback_text, raw, max_chars=max_chars)

    segments: list[Segment] = []
    current: list[TimedWord] = []
    for word in timed_words:
        current.append(word)
        text = _join_word_text([item.text for item in current])
        if SENTENCE_END_RE.search(text) or len(text) >= max_chars:
            segments.append(_segment_from_words(index_start + len(segments), current, segment_start, segment_end, raw))
            current = []
    if current:
        segments.append(_segment_from_words(index_start + len(segments), current, segment_start, segment_end, raw))
    return segments


def split_text_segment(
    index_start: int,
    start: float,
    end: float,
    text: str,
    raw: dict,
    max_chars: int = 120,
) -> list[Segment]:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return []
    pieces = _sentence_pieces(text, max_chars=max_chars)
    if len(pieces) <= 1:
        return [Segment(index=index_start, start=start, end=end, text=text, raw=raw)]

    total_weight = sum(max(1, len(piece)) for piece in pieces)
    duration = max(0.001, end - start)
    cursor = start
    segments: list[Segment] = []
    for piece in pieces:
        weight = max(1, len(piece)) / total_weight
        piece_end = end if len(segments) == len(pieces) - 1 else cursor + duration * weight
        segments.append(
            Segment(
                index=index_start + len(segments),
                start=cursor,
                end=piece_end,
                text=piece,
                raw={**raw, "timing": "estimated"},
            )
        )
        cursor = piece_end
    return segments


def _coerce_word(word: Any) -> TimedWord:
    raw = _word_raw(word)
    text = str(getattr(word, "word", "") or raw.get("word") or raw.get("text") or "")
    start = _optional_float(getattr(word, "start", None) if hasattr(word, "start") else raw.get("start"))
    end = _optional_float(getattr(word, "end", None) if hasattr(word, "end") else raw.get("end"))
    return TimedWord(text=text, start=start, end=end, raw=raw)


def _word_raw(word: Any) -> dict:
    if isinstance(word, dict):
        return dict(word)
    raw: dict = {}
    for key in ("word", "text", "start", "end", "probability"):
        if hasattr(word, key):
            raw[key] = getattr(word, key)
    return raw


def _segment_from_words(
    index: int,
    words: list[TimedWord],
    fallback_start: float,
    fallback_end: float,
    raw: dict,
) -> Segment:
    starts = [word.start for word in words if word.start is not None]
    ends = [word.end for word in words if word.end is not None]
    return Segment(
        index=index,
        start=starts[0] if starts else fallback_start,
        end=ends[-1] if ends else fallback_end,
        text=_join_word_text([word.text for word in words]),
        raw={**raw, "words": [word.raw for word in words], "timing": "word"},
    )


def _sentence_pieces(text: str, max_chars: int) -> list[str]:
    pieces = [match.group(1).strip() for match in SENTENCE_SPLIT_RE.finditer(text) if match.group(1).strip()]
    if not pieces:
        pieces = [text]
    expanded: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            expanded.append(piece)
            continue
        for start in range(0, len(piece), max_chars):
            expanded.append(piece[start : start + max_chars].strip())
    return [piece for piece in expanded if piece]


def _join_word_text(words: list[str]) -> str:
    text = "".join(words).strip()
    return re.sub(r"\s+", " ", text)


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
