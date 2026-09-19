"""
Tests for the AI Editor caption-source logic.

Primarily verifies Task 1: ``get_clip_transcript`` reuses the stored full-video
transcript (sliced + rebased to the clip's window) instead of re-running
Whisper, and signals a fallback when no stored transcript covers the clip range.
"""
import pytest

from backend.services.auto_editor import (
    _clamp_ellipsis,
    _fit_caption,
    _rebase_transcript_segments,
    _resolve_caption_source,
    _sanitize_segments,
    _validate_ass_file,
    _wrap_text,
    get_clip_transcript,
)


class _Clip:
    """Minimal Clip stand-in exposing the fields auto_editor reads."""

    def __init__(self, start_time: float, end_time: float):
        self.start_time = start_time
        self.end_time = end_time
        self.duration = end_time - start_time


class _Transcript:
    """Minimal Transcript stand-in exposing content_json / word_timestamps_json."""

    def __init__(self, content=None, words=None):
        self.content_json = content
        self.word_timestamps_json = words


# A full-video transcript spanning 0-100s with several segments/words.
_FULL_SEGMENTS = [
    {"start": 0.0,  "end": 10.0, "text": "intro"},
    {"start": 29.5, "end": 32.0, "text": "clip start word"},  # straddles left edge
    {"start": 35.0, "end": 40.0, "text": "hello world"},      # fully inside
    {"start": 58.0, "end": 62.0, "text": "clip end word"},    # straddles right edge
    {"start": 90.0, "end": 95.0, "text": "outside clip"},
]
_FULL_WORDS = [
    {"word": "a", "start": 30.0, "end": 31.0},
    {"word": "hello", "start": 35.0, "end": 36.0},
]


class TestRebaseTranscriptSegments:
    def test_filters_and_rebases(self):
        segs = _rebase_transcript_segments(_FULL_SEGMENTS, 30.0, 60.0)
        assert [s["text"] for s in segs] == [
            "clip start word", "hello world", "clip end word",
        ]
        # left-straddle trimmed to window and rebased to 0
        assert segs[0]["start"] == 0.0 and segs[0]["end"] == 2.0
        # fully-inside rebased: 35-40 -> 5-10
        assert segs[1]["start"] == 5.0 and segs[1]["end"] == 10.0
        # right-straddle clamped to clip end and rebased: 58-62 -> 28-30
        assert segs[2]["start"] == 28.0 and segs[2]["end"] == 30.0

    def test_empty_input(self):
        assert _rebase_transcript_segments([], 0.0, 10.0) == []
        assert _rebase_transcript_segments(None, 0.0, 10.0) == []

    def test_invalid_window(self):
        assert _rebase_transcript_segments(_FULL_SEGMENTS, 60.0, 60.0) == []


class TestGetClipTranscript:
    def test_reuses_stored_transcript(self):
        """Happy path: stored transcript covers the clip -> sliced + rebased,
        with NO attempt to re-transcribe (no transcript call made here at all)."""
        clip = _Clip(30.0, 60.0)
        tr = _Transcript(content=_FULL_SEGMENTS, words=_FULL_WORDS)
        segs, words = get_clip_transcript(clip, tr)

        assert [s["text"] for s in segs] == [
            "clip start word", "hello world", "clip end word",
        ]
        assert segs[0] == {"start": 0.0, "end": 2.0, "text": "clip start word"}
        assert words[0] == {"word": "a", "start": 0.0, "end": 1.0}

    def test_no_transcript_returns_none_none(self):
        clip = _Clip(30.0, 60.0)
        assert get_clip_transcript(clip, None) == (None, None)

    def test_no_coverage_signals_fallback(self):
        """Re-trimmed clip outside the stored transcript -> (None, None), which
        is the signal auto_edit_clip uses to trigger fresh Whisper fallback."""
        clip = _Clip(200.0, 230.0)
        tr = _Transcript(content=_FULL_SEGMENTS, words=_FULL_WORDS)
        assert get_clip_transcript(clip, tr) == (None, None)

    def test_tolerates_wrapped_dict_content(self):
        clip = _Clip(30.0, 60.0)
        tr = _Transcript(content={"segments": _FULL_SEGMENTS}, words=_FULL_WORDS)
        segs, _w = get_clip_transcript(clip, tr)
        assert [s["text"] for s in segs] == [
            "clip start word", "hello world", "clip end word",
        ]

    def test_word_level_only(self):
        """Transcript with only word timestamps still produces rebased words."""
        clip = _Clip(30.0, 60.0)
        tr = _Transcript(content=None, words=_FULL_WORDS)
        segs, words = get_clip_transcript(clip, tr)
        assert segs == []
        assert words[0] == {"word": "a", "start": 0.0, "end": 1.0}


class TestNestedWordRebase:
    """Karaoke hardening: word-level timestamps NESTED inside a segment must be
    rebased by clip_start too, so karaoke highlighting stays in sync with the
    clip-relative timeline (not absolute full-video offsets)."""

    # Segment at absolute 34-39 with nested words, cover 35-38 inside clip.
    _SEG_WITH_WORDS = [
        {
            "start": 34.0,
            "end": 39.0,
            "text": "hello nested world",
            "words": [
                {"word": "hello", "start": 35.0, "end": 36.0},
                {"word": "nested", "start": 36.0, "end": 37.5},
                {"word": "world", "start": 37.5, "end": 38.0},
            ],
        }
    ]

    def test_nested_words_rebased(self):
        segs = _rebase_transcript_segments(self._SEG_WITH_WORDS, 30.0, 60.0)
        assert len(segs) == 1
        seg = segs[0]
        # Segment boundaries rebased to 0-based
        assert seg["start"] == 4.0 and seg["end"] == 9.0
        # Nested words rebased by the SAME clip_start (30 -> 0)
        assert "words" in seg  # nested words preserved & rebased now
        assert seg["words"] == [
            {"word": "hello", "start": 5.0, "end": 6.0},
            {"word": "nested", "start": 6.0, "end": 7.5},
            {"word": "world", "start": 7.5, "end": 8.0},
        ]

    def test_nested_words_clipped_to_clip_edge(self):
        """A word straddling the left clip boundary is trimmed to start at 0,
        not left at an absolute (pre-clip) offset."""
        seg = {
            "start": 29.0,
            "end": 33.0,
            "text": "straddle",
            "words": [
                {"word": "strad", "start": 29.5, "end": 30.5},
                {"word": "dle", "start": 30.5, "end": 32.0},
            ],
        }
        segs = _rebase_transcript_segments([seg], 30.0, 60.0)
        assert segs[0]["start"] == 0.0 and segs[0]["end"] == 3.0
        # strad starts 29.5 (< 30) -> trimmed to clip_start, rebased to 0
        assert segs[0]["words"][0]["start"] == 0.0
        assert segs[0]["words"][0]["end"] == 0.5
        assert segs[0]["words"][1] == {"word": "dle", "start": 0.5, "end": 2.0}

    def test_nested_word_dropped_when_zero_width_after_rebase(self):
        """A nested word that collapses to zero width after rebasing is dropped
        (like zero-width segments are), while valid sibling words survive."""
        seg = {
            "start": 35.0,
            "end": 40.0,
            "text": "good bad",
            "words": [
                {"word": "good", "start": 35.0, "end": 36.0},
                {"word": "bad", "start": 37.0, "end": 37.0},  # zero-width
            ],
        }
        segs = _rebase_transcript_segments([seg], 30.0, 60.0)
        assert len(segs) == 1
        assert [w["word"] for w in segs[0]["words"]] == ["good"]
        assert segs[0]["words"][0] == {"word": "good", "start": 5.0, "end": 6.0}

    def test_get_clip_transcript_rebases_nested_words(self):
        """End-to-end: get_clip_transcript returns a segment whose nested words
        are in clip-relative time (this is what feeds karaoke highlight)."""
        clip = _Clip(30.0, 60.0)
        tr = _Transcript(content=self._SEG_WITH_WORDS)
        segs, _w = get_clip_transcript(clip, tr)
        assert len(segs) == 1
        assert segs[0]["words"] == [
            {"word": "hello", "start": 5.0, "end": 6.0},
            {"word": "nested", "start": 6.0, "end": 7.5},
            {"word": "world", "start": 7.5, "end": 8.0},
        ]


_VALID_ASS = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&HFFFFFF,&H000000FF,&H000000,&H80000000,-1,0,0,0,100,100,1,0,1,3,4,2,40,40,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\kf100}hello world
Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,{\\kf80}second cue
"""


class TestValidateAssFile:
    def test_valid_ass(self, tmp_path):
        p = tmp_path / "ok.ass"
        p.write_text(_VALID_ASS, encoding="utf-8")
        ok, err = _validate_ass_file(p)
        assert ok is True, err
        assert err == ""

    def test_missing_file(self, tmp_path):
        ok, err = _validate_ass_file(tmp_path / "nope.ass")
        assert ok is False
        assert "does not exist" in err

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.ass"
        p.write_text("", encoding="utf-8")
        ok, _err = _validate_ass_file(p)
        assert ok is False

    def test_missing_script_info(self, tmp_path):
        p = tmp_path / "no_info.ass"
        p.write_text("[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hi\n", encoding="utf-8")
        ok, err = _validate_ass_file(p)
        assert ok is False
        assert "[Script Info]" in err

    def test_missing_events(self, tmp_path):
        p = tmp_path / "no_events.ass"
        p.write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
        ok, err = _validate_ass_file(p)
        assert ok is False
        assert "[Events]" in err

    def test_zero_duration_cue_rejected(self, tmp_path):
        # start == end -> invalid (would render nothing / cause timing issues)
        p = tmp_path / "zero.ass"
        content = _VALID_ASS.replace("0:00:01.00,0:00:03.50", "0:00:02.00,0:00:02.00")
        p.write_text(content, encoding="utf-8")
        ok, err = _validate_ass_file(p)
        assert ok is False
        assert "duration" in err

    def test_invalid_no_dialogue(self, tmp_path):
        p = tmp_path / "nodial.ass"
        p.write_text(
            "[Script Info]\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n",
            encoding="utf-8",
        )
        ok, err = _validate_ass_file(p)
        assert ok is False
        assert "no Dialogue" in err


class TestSanitizeReasonLogging:
    """_sanitize_segments must drop hallucinations/confidence-failures and keep
    real content — and count removals per reason (used for diagnostics)."""

    def test_drops_no_speech_high_confidence(self):
        segs = [
            {"start": 0.0, "end": 2.0, "text": "hello", "no_speech_prob": 0.99},
            {"start": 2.0, "end": 4.0, "text": "real content", "no_speech_prob": 0.01},
            {"start": 4.0, "end": 6.0, "text": "thank you", "no_speech_prob": 0.02},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert [s["text"] for s in clean] == ["real content"]

    def test_keeps_content_when_confidence_fields_absent(self):
        # whisper.cpp / reused transcripts have no confidence fields -> must NOT
        # be dropped. Only the known-phrase hallucination "hello" is removed.
        segs = [
            {"start": 0.0, "end": 2.0, "text": "hello"},
            {"start": 2.0, "end": 5.0, "text": "this is a real sentence"},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert [s["text"] for s in clean] == ["this is a real sentence"]

    def test_low_logprob_garbage_dropped(self):
        # A very low avg_logprob (-3.0) marks near-certain fabrication and is
        # dropped even when no_speech_prob is absent (the `not has_no_speech`
        # branch short-circuits).  A normal-confidence cue is always kept.
        segs = [
            {"start": 0.0, "end": 3.0, "text": "uncertain garbage", "avg_logprob": -3.0},
            {"start": 3.0, "end": 6.0, "text": "solid", "avg_logprob": -0.2, "no_speech_prob": 0.01},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert [s["text"] for s in clean] == ["solid"]

    def test_preserves_nested_words(self):
        # _sanitize_segments must PRESERVE nested word timestamps on kept cues
        # (they feed karaoke highlighting); only the hallucinated cue is dropped.
        segs = [
            {"start": 0.0, "end": 3.0, "text": "real line",
             "words": [{"word": "real", "start": 0.0, "end": 1.0},
                       {"word": "line", "start": 1.0, "end": 3.0}]},
            {"start": 3.0, "end": 5.0, "text": "thank you"},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert len(clean) == 1
        assert "words" in clean[0]
        assert clean[0]["words"][0] == {"word": "real", "start": 0.0, "end": 1.0}

    def test_prefix_filter_requires_word_boundary(self):
        # Regression: the hallucination "prefix" filter must NOT drop real words
        # that merely START with a single-token hallucination ("so", "a", "we",
        # "the", "you", "i").  "solid"⊃"so", "awesome"⊃"a", "their"⊃"the",
        # "weekend"⊃"we" are all legitimate text and must be kept.
        segs = [
            {"start": 0.0, "end": 2.0, "text": "solid"},
            {"start": 2.0, "end": 4.0, "text": "awesome"},
            {"start": 4.0, "end": 6.0, "text": "their"},
            {"start": 6.0, "end": 8.0, "text": "weekend"},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert [s["text"] for s in clean] == ["solid", "awesome", "their", "weekend"]

    def test_prefix_filter_still_catches_phrase(self):
        # But a genuine *phrase* prefix ("thank you") on filler text (under the
        # 35-char guard) is still removed.
        segs = [
            {"start": 0.0, "end": 3.0, "text": "thank you for watching please"},
            {"start": 3.0, "end": 6.0, "text": "real sentence"},
        ]
        clean = _sanitize_segments(segs, clip_duration=10.0)
        assert [s["text"] for s in clean] == ["real sentence"]


class TestTextWidthFitting:
    """drawtext has NO width awareness, so long captions ran off both frame
    edges ("secret imposter sabotages and commits n[cut off]").  These tests
    pin the new measure + wrap + hard-clamp logic so no line can overflow."""

    @staticmethod
    def _m(width_per_char: float = 8.0):
        def _m(t: str) -> float:
            return width_per_char * len(str(t or ""))
        return _m

    def test_short_text_stays_single_line(self):
        m = self._m()
        assert _wrap_text("hello world", m, 200) == ["hello world"]

    def test_long_text_wraps_to_two_lines(self):
        m = self._m()
        text = "a quick brown fox jumps over that lazy dog while still running"
        lines = _wrap_text(text, m, 200)
        assert 2 <= len(lines) <= 2  # capped at 2 lines
        for ln in lines:
            assert m(ln) <= 200  # every line fits the usable width

    def test_single_too_wide_word_hard_clamped_with_ellipsis(self):
        m = self._m()
        word = "supercalifragilisticexpialidociouslongword"
        lines = _wrap_text(word, m, 60)
        assert lines
        # The hard-clamp guarantees the overflowed word is ellipsized, NOT cut
        # off at the frame edge.
        assert all(m(ln) <= 60 for ln in lines)
        assert lines[0].endswith("…")

    def test_clamp_ellipsis_never_exceeds_width(self):
        m = self._m()
        out = _clamp_ellipsis("antidisestablishmentarianism", m, 40)
        assert m(out) <= 40
        assert out.startswith("antidis")

    def test_cjk_no_space_breaks_by_character(self):
        # CJK has no word spaces; wrapping must break mid-run, never drop.
        m = self._m(20.0)  # wide chars
        long_cjk = "这是" * 20
        lines = _wrap_text(long_cjk, m, 100)
        assert 1 <= len(lines) <= 2
        assert all(m(ln) <= 100 for ln in lines)

    def test_fit_caption_reduces_fontsize_for_long_text(self):
        # _fit_caption should shrink the font (or clamp) so the text fits the
        # usable canvas instead of overflowing at the base size.
        size, lines = _fit_caption(
            "introducing the secret imposter who sabotages the whole mission and commits",
            "/nonexistent/font.ttf",  # triggers the conservative estimate path
            max_width=972, base_size=60, min_size=30,
        )
        assert size <= 60
        assert lines
        assert len(lines) <= 2

    def test_fit_caption_empty_returns_min_size_capacity(self):
        # Empty input degrades gracefully (returns a usable size, no crash) —
        # callers skip empty cues before fitting, so this is defensive.
        size, lines = _fit_caption("", "/nonexistent/font.ttf", 500)
        assert size == 30
        assert isinstance(lines, list)


class TestResolveCaptionSource:
    """The 2026-09-12 "early subtitles" fix: FRESH transcription of the actual
    rendered clip is ALWAYS preferred over reusing the stored full-video
    transcript.  Reuse rebases against the DB start_time, which is NOT the
    clip's true first frame (FFmpeg keyframe-snaps ``-c copy`` cuts to the
    keyframe BEFORE start_time), so reused captions land EARLY.  Reuse must
    only be a last resort."""

    def test_fresh_wins_over_reuse_even_when_reuse_available(self):
        # Both sources produced segments — fresh MUST win, because only fresh is
        # timeline-exact against the rendered clip.
        _fresh = [{"start": 0.0, "end": 1.0, "text": "exact"}]
        _reuse = [{"start": 1.8, "end": 2.8, "text": "offset-early"}]
        src, segs, _w = _resolve_caption_source(_fresh, [], _reuse, [])
        assert src == "fresh"
        assert segs is _fresh  # identity: caller keeps the fresh output

    def test_fresh_words_only_still_wins(self):
        # fresh has words (no segments) and reuse has segments -> fresh wins.
        src, segs, words = _resolve_caption_source([], [{"word": "a"}], [{"start": 0.0}], [])
        assert src == "fresh"
        assert segs == []
        assert len(words) == 1

    def test_reuse_only_when_fresh_empty(self):
        # Only fall back to reuse when fresh produced nothing at all.
        _reuse = [{"start": 0.0, "end": 1.0, "text": "fallback"}]
        src, segs, _w = _resolve_caption_source([], [], _reuse, [])
        assert src == "reuse"
        assert segs is _reuse

    def test_none_when_both_empty(self):
        src, segs, words = _resolve_caption_source([], [], None, None)
        assert src == "none"
        assert segs == [] and words == []

    def test_reuse_nonempty_but_fresh_segs_empty_words_small(self):
        # Make sure we don't accidentally prefer reuse over a tiny fresh result.
        src, _s, _w = _resolve_caption_source([{"start": 0.0, "end": 0.1, "text": "x"}], [], [{"start": 9.0}], [])
        assert src == "fresh"
