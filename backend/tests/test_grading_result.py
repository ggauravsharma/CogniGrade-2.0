"""Regression tests for the structured grading result contract.

THE BUG THESE PROTECT AGAINST
-----------------------------
Grading used to parse free text::

    Grade: 3
    Reason: prose

with `except ValueError: pass`, so every malformed response produced a silent
`None` score. Re-evaluation then wrote that `None` straight over a student's
existing marks, and exam aggregation counts NULL as a zero contribution while
still stamping the exam "graded".

A model response is untrusted input. "I could not understand the response" must
never become "the student scored zero".

No network call and no API quota: the provider is stubbed throughout.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from backend.grading.result import (
    SCORE_EPSILON,
    GradingResponseError,
    GradingResult,
    build_grading_result,
    output_instruction,
    parse_grading_response,
)

# ---------------------------------------------------------------------------
# valid outputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score", [0, 0.5, 1.5, 2.25, 5])
def test_valid_scores_including_partial_credit(score):
    """Partial credit must be representable in the domain, whatever the DB does."""
    r = build_grading_result(score=score, reason="ok", max_marks=5)
    assert r.score == pytest.approx(float(score))
    assert isinstance(r.score, float)


def test_zero_is_a_valid_grade_not_a_failure():
    r = build_grading_result(score=0, reason="nothing correct", max_marks=5)
    assert r.score == 0.0
    assert r.reason == "nothing correct"


def test_score_equal_to_max_is_valid():
    assert build_grading_result(score=5, reason="perfect", max_marks=5).score == 5.0


def test_numeric_string_score_is_accepted():
    assert build_grading_result(score="2.5", reason="half", max_marks=5).score == 2.5


def test_fraction_string_uses_numerator():
    """The old prompt invited "3/5"; tolerated, but only the numerator counts."""
    assert build_grading_result(score="3/5", reason="ok", max_marks=5).score == 3.0


def test_reason_is_stripped():
    assert build_grading_result(score=1, reason="  spaced  ", max_marks=5).reason == "spaced"


# ---------------------------------------------------------------------------
# invalid outputs -- every one must raise, never return
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad,code", [
    (None, "score_missing"),
    ("", "score_missing"),
    ("hello", "score_not_numeric"),
    ("abc", "score_not_numeric"),
    ([], "score_not_numeric"),
    ({}, "score_not_numeric"),
    (True, "score_not_numeric"),
    (float("nan"), "score_not_finite"),
    (float("inf"), "score_not_finite"),
    (float("-inf"), "score_not_finite"),
    ("nan", "score_not_finite"),
    ("inf", "score_not_finite"),
    (-1, "score_negative"),
    (-0.5, "score_negative"),
    (6, "score_above_max"),
    (99, "score_above_max"),
])
def test_invalid_scores_raise_with_a_code(bad, code):
    with pytest.raises(GradingResponseError) as exc:
        build_grading_result(score=bad, reason="r", max_marks=5)
    assert exc.value.code == code


def test_nan_would_have_passed_the_old_range_check():
    """`nan < 0` and `nan > max` are both False, so the old guard let it through."""
    nan = float("nan")
    assert not (nan < 0) and not (nan > 5)      # the old condition
    with pytest.raises(GradingResponseError):    # the new one
        build_grading_result(score=nan, reason="r", max_marks=5)


def test_boolean_is_not_a_score():
    """`True` is an int in Python; grading a student 1 for `true` would be absurd."""
    with pytest.raises(GradingResponseError) as exc:
        build_grading_result(score=True, reason="r", max_marks=5)
    assert exc.value.code == "score_not_numeric"


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_reason_raises(reason):
    with pytest.raises(GradingResponseError) as exc:
        build_grading_result(score=1, reason=reason, max_marks=5)
    assert exc.value.code == "reason_missing"


# ---------------------------------------------------------------------------
# bounds and tolerance
# ---------------------------------------------------------------------------


def test_score_within_float_tolerance_is_snapped_not_rejected():
    r = build_grading_result(score=5 + SCORE_EPSILON / 2, reason="ok", max_marks=5)
    assert r.score == 5.0, "a representation-error overshoot snaps to max"


def test_score_beyond_tolerance_is_rejected_not_clamped():
    with pytest.raises(GradingResponseError) as exc:
        build_grading_result(score=5.5, reason="ok", max_marks=5)
    assert exc.value.code == "score_above_max", "a wild score must fail, not clamp"


def test_no_max_marks_means_only_lower_bound_enforced():
    assert build_grading_result(score=1000, reason="ok", max_marks=None).score == 1000.0
    with pytest.raises(GradingResponseError):
        build_grading_result(score=-1, reason="ok", max_marks=None)


def test_unusable_max_marks_is_ignored_rather_than_crashing():
    r = build_grading_result(score=3, reason="ok", max_marks="not a number")
    assert r.score == 3.0
    assert r.max_marks is None


# ---------------------------------------------------------------------------
# response decoding
# ---------------------------------------------------------------------------


def test_plain_json_response():
    r = parse_grading_response('{"score": 2.5, "reason": "half credit"}', max_marks=5)
    assert r.score == 2.5 and r.reason == "half credit"


def test_fenced_json_is_tolerated():
    """If the provider ignores the JSON mime type and fences the body."""
    raw = '```json\n{"score": 1, "reason": "ok"}\n```'
    assert parse_grading_response(raw, max_marks=5).score == 1.0


def test_json_with_surrounding_prose_is_tolerated():
    raw = 'Here is my assessment:\n{"score": 4, "reason": "good"}\nHope that helps.'
    assert parse_grading_response(raw, max_marks=5).score == 4.0


@pytest.mark.parametrize("raw,code", [
    (None, "empty_response"),
    ("", "empty_response"),
    ("   ", "empty_response"),
    ("I cannot grade this.", "not_json"),
    ("Grade: 3\nReason: prose", "not_json"),
    ('{"score": 3, "reason":', "malformed_json"),
    ('[1, 2, 3]', "not_json"),
    ('{"reason": "no score here"}', "score_missing"),
    ('{"score": "abc", "reason": "r"}', "score_not_numeric"),
    ('{"score": -2, "reason": "r"}', "score_negative"),
    ('{"score": 99, "reason": "r"}', "score_above_max"),
    ('{"score": 3}', "reason_missing"),
])
def test_malformed_responses_raise_with_a_code(raw, code):
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response(raw, max_marks=5)
    assert exc.value.code == code, f"{raw!r} -> {exc.value.code}"


def test_json_scalar_is_wrong_schema():
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response("42", max_marks=5)
    assert exc.value.code in ("not_json", "wrong_schema")


def test_failure_carries_truncated_raw_body_for_debugging():
    raw = '{"score": -1, "reason": "r"}'
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response(raw, max_marks=5)
    assert exc.value.raw is not None and "score" in exc.value.raw
    long = '{"score": -1, "reason": "' + "x" * 5000 + '"}'
    with pytest.raises(GradingResponseError) as exc2:
        parse_grading_response(long, max_marks=5)
    assert len(exc2.value.raw) <= 2000, "raw body must be truncated in the error"


# ---------------------------------------------------------------------------
# historical regression: the old parser's silent-None path
# ---------------------------------------------------------------------------


OLD_PARSER_SILENT_NONE_CASES = [
    "The student did well overall.",                 # no "Grade:" marker at all
    "Grade: excellent\nReason: good work",           # float() raised ValueError
    "Grade: -3\nReason: negative",                   # out of range -> None
    "Grade: 9999\nReason: above max",                # out of range -> None
    "Grade: nan\nReason: not a number",              # NaN slipped THROUGH to the DB
]


@pytest.mark.parametrize("raw", OLD_PARSER_SILENT_NONE_CASES)
def test_responses_that_used_to_become_silent_none_now_raise(raw):
    """Each of these previously produced `grade = None` and no error.

    `except ValueError: pass` swallowed the parse failure, the out-of-range
    guard reset the score to None, and the DB write was then skipped -- leaving
    the mark NULL with nothing recorded anywhere.
    """
    with pytest.raises(GradingResponseError):
        parse_grading_response(raw, max_marks=5)


def test_old_parser_would_have_returned_none_for_these():
    """Demonstrates the old behaviour explicitly, so the contrast is testable."""
    def old_parser(result_text, max_marks):
        grade = None
        if "Grade:" in result_text:
            grade_line = [ln for ln in result_text.split("\n") if "Grade:" in ln][0]
            grade_str = grade_line.split("Grade:")[1].strip()
            try:
                grade = float(grade_str.split()[0].split("/")[0])
            except ValueError:
                pass
        if grade is not None and (grade < 0 or grade > max_marks):
            grade = None
        return grade

    assert old_parser("The student did well overall.", 5) is None
    assert old_parser("Grade: excellent\nReason: x", 5) is None
    assert old_parser("Grade: -3\nReason: x", 5) is None
    # and the one that was worse than None: NaN reached the database
    assert math.isnan(old_parser("Grade: nan\nReason: x", 5))


# ---------------------------------------------------------------------------
# provider independence  (Phase L)
# ---------------------------------------------------------------------------


def test_grading_result_needs_no_provider_import():
    """The contract must be constructible with no AI SDK present."""
    r = GradingResult(score=1.5, reason="ok", max_marks=5)
    assert r.score == 1.5


@pytest.mark.parametrize("module", ["backend.grading.result", "backend.grading.evidence"])
def test_grading_domain_modules_import_no_provider(module):
    """Token-level check: comments and docstrings may name providers, code may not."""
    import importlib
    import tokenize

    mod = importlib.import_module(module)
    with open(mod.__file__, "rb") as fh:
        code = " ".join(
            t.string for t in tokenize.tokenize(fh.readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING,
                              tokenize.NL, tokenize.NEWLINE)
        )
    for banned in ("genai", "google", "gemini", "Gemini", "fastapi",
                   "sqlalchemy", "HTTPException"):
        assert banned not in code, f"{module} must not reference {banned} in code"


def test_output_instruction_is_short_and_names_the_bound():
    text = output_instruction(5)
    assert "score" in text and "reason" in text and "5" in text
    assert "markdown" not in text.lower() and "```" not in text
    assert len(text) < 200, "the output contract must stay cheap in tokens"


# ---------------------------------------------------------------------------
# both grading routes resolve to the same contract  (Phase G)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalars(self):
        return self

    def first(self):
        return self._obj


class _DB:
    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.committed = False

    async def execute(self, *a, **kw):
        return _Result(self._sequence.pop(0))

    async def commit(self):
        self.committed = True


def _question(max_marks=5):
    return SimpleNamespace(
        id=3, question_number=1, text="Q", max_marks=max_marks,
        ideal_answer="ideal", ideal_marking_scheme="scheme",
        ms_text_images=None, ms_table_images=None, ms_diagram_images=None,
    )


def _response():
    return SimpleNamespace(
        answer_text="student answer", marks_obtained=None, reasoning=None,
        ans_text_images=None, ans_table_images=None, ans_diagram_images=None,
    )


def _stub_model(monkeypatch, geminiAPI, body):
    captured = {}

    class FakeModel:
        def generate_content(self, prompt, generation_config=None):
            captured["prompt"] = prompt
            captured["generation_config"] = generation_config
            return SimpleNamespace(text=body)

    monkeypatch.setattr(geminiAPI, "get_model", lambda: FakeModel())
    return captured


@pytest.mark.asyncio
async def test_text_grading_returns_validated_result(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    _stub_model(monkeypatch, geminiAPI, '{"score": 2.5, "reason": "half"}')
    db = _DB([q, qr])

    out = await geminiAPI.grade_question(
        {"student_answer": "a", "ideal_answer": "i", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "graded"
    assert out["grade"] == 2.5
    assert qr.marks_obtained == 2.5, "a validated score must be persisted"


@pytest.mark.asyncio
async def test_text_grading_failure_does_not_write_marks(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    qr.marks_obtained = 4          # a previously good mark
    _stub_model(monkeypatch, geminiAPI, "I am unable to grade this answer.")
    db = _DB([q, qr])

    out = await geminiAPI.grade_question(
        {"student_answer": "a", "ideal_answer": "i", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "grading_failed"
    assert out["grade"] is None
    assert out["error_code"] == "not_json"
    assert qr.marks_obtained == 4, "a provider failure must not touch the mark"


@pytest.mark.asyncio
async def test_diagram_grading_returns_the_same_contract(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    _stub_model(monkeypatch, geminiAPI, '{"score": 1.5, "reason": "partial"}')
    db = _DB([qr, q, qr])

    out = await geminiAPI.grade_question_with_diagram(
        {"ideal_answer": "i", "marking_scheme": "m",
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "graded"
    assert out["grade"] == 1.5
    assert qr.marks_obtained == 1.5


@pytest.mark.asyncio
async def test_diagram_grading_failure_is_explicit(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    qr.marks_obtained = 3
    _stub_model(monkeypatch, geminiAPI, '{"score": 500, "reason": "way too high"}')
    db = _DB([qr, q, qr])

    out = await geminiAPI.grade_question_with_diagram(
        {"ideal_answer": "i", "marking_scheme": "m",
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "grading_failed"
    assert out["error_code"] == "score_above_max"
    assert qr.marks_obtained == 3, "an out-of-range score must not overwrite marks"


@pytest.mark.asyncio
async def test_json_output_is_requested_from_the_provider(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    captured = _stub_model(monkeypatch, geminiAPI, '{"score": 1, "reason": "ok"}')
    db = _DB([q, qr])

    await geminiAPI.grade_question(
        {"student_answer": "a", "ideal_answer": "i", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    cfg = captured["generation_config"]
    assert cfg["response_mime_type"] == "application/json"
    assert "Grade:" not in captured["prompt"], "the free-text contract is gone"
    assert "score" in captured["prompt"]


@pytest.mark.asyncio
async def test_empty_provider_response_is_explicit_failure(monkeypatch):
    from backend.routers import geminiAPI

    q = _question()
    qr = _response()
    _stub_model(monkeypatch, geminiAPI, "")
    db = _DB([q, qr])

    out = await geminiAPI.grade_question(
        {"student_answer": "a", "ideal_answer": "i", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "grading_failed"
    assert out["error_code"] == "empty_response"


@pytest.mark.asyncio
async def test_provider_text_accessor_raising_is_handled(monkeypatch):
    """`response.text` raises on a blocked candidate; that became an opaque 500."""
    from backend.routers import geminiAPI

    class Exploding:
        @property
        def text(self):
            raise RuntimeError("no candidates")

    class FakeModel:
        def generate_content(self, prompt, generation_config=None):
            return Exploding()

    monkeypatch.setattr(geminiAPI, "get_model", lambda: FakeModel())
    q = _question()
    qr = _response()
    db = _DB([q, qr])

    out = await geminiAPI.grade_question(
        {"student_answer": "a", "ideal_answer": "i", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        db, None,
    )
    assert out["status"] == "grading_failed"
    assert out["error_code"] == "empty_response"
