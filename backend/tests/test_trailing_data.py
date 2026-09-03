"""The grading decoder's object boundary: what may surround the one result.

WHY THIS FILE EXISTS
--------------------
Live exam-4 Q31 was blamed on a provider timeout for two rounds of diagnosis.
A single attempt with retries disabled and a 180-second budget returned in 16.1
seconds with a successful finish and a 407-character body, which the decoder
then refused:

    response was not valid JSON: Extra data: line 1 column 407 (char 406)

`Extra data` is only producible by a body that CLOSED. A response cut off at an
output limit raises `Unterminated string` or `Expecting value` on an object that
never ended, so truncation was never the mechanism: the provider returned a
complete grading object followed by one stray character, and the old
`_extract_json_object` handed the whole text -- object plus stray character --
straight to `json.loads` because it started with `{`. Its object-extraction
fallback only ran when the text did NOT start with an object, so it could never
reach the bodies that needed it.

The fix is structural, not permissive. `JSONDecoder.raw_decode` consumes exactly
one complete JSON value and reports where it ended; everything after that end
position is inspected explicitly. No regex brace-matching, no score parsed out
of prose, and every accepted object still goes through the unchanged strict
validation.

The live body is NOT reproduced here. It contains a model's assessment of a real
student's answer. Every case below is synthetic and carries the SHAPE only.

No network, no provider, no quota.
"""

from __future__ import annotations

import json

import pytest

from backend.grading.failure import FAILURE_MESSAGES, describe
from backend.grading.result import GradingResponseError, parse_grading_response

MAX = 5

OBJECT = '{"score": 3, "reason": "clear working"}'

FENCE = "```"


def _code(raw, *, max_marks=MAX):
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response(raw, max_marks=max_marks)
    return exc.value.code


# ---------------------------------------------------------------------------
# ACCEPTED -- previously supported shapes that must not regress
# ---------------------------------------------------------------------------

def test_exact_object():
    result = parse_grading_response(OBJECT, max_marks=MAX)
    assert (result.score, result.reason) == (3.0, "clear working")


def test_leading_and_trailing_whitespace():
    assert parse_grading_response("\n\t  " + OBJECT + "  \n\n", max_marks=MAX).score == 3.0


def test_fenced_json_block():
    raw = FENCE + "json\n" + OBJECT + "\n" + FENCE
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


def test_fenced_block_without_a_language_tag():
    raw = FENCE + "\n" + OBJECT + "\n" + FENCE
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


def test_prose_before_the_object():
    raw = "Here is my assessment of the answer.\n" + OBJECT
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


# ---------------------------------------------------------------------------
# ACCEPTED -- the shape that was failing in production
# ---------------------------------------------------------------------------

def test_one_stray_trailing_character():
    """The live Q31 shape: a complete object, then a single extra character."""
    assert parse_grading_response(OBJECT + "}", max_marks=MAX).score == 3.0
    assert parse_grading_response(OBJECT + "`", max_marks=MAX).score == 3.0


def test_trailing_prose():
    raw = OBJECT + "\nI hope this assessment is helpful."
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


def test_trailing_prose_containing_a_bare_number():
    """A number in a trailing sentence decodes as JSON. It is not a second result.

    Rejecting on any decodable trailing VALUE would throw a valid grade away
    over ordinary prose, which is why the ambiguity check looks for objects.
    """
    raw = OBJECT + "\nTotal awarded: 3 out of 5 marks."
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


def test_trailing_closing_fence_when_the_body_started_bare():
    """Only a LEADING fence is stripped; a lone trailing one must be harmless."""
    assert parse_grading_response(OBJECT + "\n" + FENCE, max_marks=MAX).score == 3.0


def test_prose_on_both_sides():
    raw = "Assessment follows.\n" + OBJECT + "\nHope that helps."
    assert parse_grading_response(raw, max_marks=MAX).score == 3.0


# ---------------------------------------------------------------------------
# ACCEPTED -- structures a regex brace-match would have broken
# ---------------------------------------------------------------------------

def test_nested_object_field():
    """A greedy brace regex over-reaches and a lazy one stops early; not the decoder."""
    raw = '{"score": 2, "reason": "partial", "breakdown": {"part_a": 1, "part_b": 1}}'
    assert parse_grading_response(raw, max_marks=MAX).score == 2.0


def test_nested_object_field_with_trailing_prose():
    raw = '{"score": 2, "reason": "partial", "breakdown": {"a": {"b": 1}}} thanks'
    assert parse_grading_response(raw, max_marks=MAX).score == 2.0


def test_braces_inside_a_string_value():
    raw = '{"score": 4, "reason": "wrote the set {x, y} and the rule {a -> b}"}'
    result = parse_grading_response(raw, max_marks=MAX)
    assert result.score == 4.0
    assert "{x, y}" in result.reason, "the reason must survive intact"


def test_an_unbalanced_brace_inside_a_string_value():
    """A stray opening brace inside the reason must not be read as a second payload."""
    raw = '{"score": 1, "reason": "the student wrote { and stopped"} trailing'
    assert parse_grading_response(raw, max_marks=MAX).score == 1.0


def test_a_json_array_field_is_not_confused_for_a_payload():
    raw = '{"score": 2.5, "reason": "ok", "criteria": [{"id": 1}, {"id": 2}]}'
    assert parse_grading_response(raw, max_marks=MAX).score == 2.5


# ---------------------------------------------------------------------------
# ACCEPTED -- grading semantics that must survive the change
# ---------------------------------------------------------------------------

def test_a_genuine_zero_survives_trailing_data():
    """A real 0 is a grade. It must never become a failure, and never a NULL."""
    result = parse_grading_response('{"score": 0, "reason": "nothing correct"} .', max_marks=MAX)
    assert result.score == 0.0


@pytest.mark.parametrize("literal,expected", [("0.5", 0.5), ("1.5", 1.5), ("2.25", 2.25)])
def test_fractional_marks_survive_trailing_data(literal, expected):
    raw = '{"score": %s, "reason": "partial"} trailing note' % literal
    assert parse_grading_response(raw, max_marks=MAX).score == expected


def test_a_numeric_string_score_still_works_with_trailing_data():
    assert parse_grading_response('{"score": "2.5", "reason": "x"} !', max_marks=MAX).score == 2.5


# ---------------------------------------------------------------------------
# REJECTED -- malformed, with the code each case has always carried
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # a broken FIRST object -- these are what `malformed_json` should mean
    ('{"score": 2, "reason": "unterminated', "malformed_json"),
    ('{"score": 2, "reason": "x",}', "malformed_json"),
    ("{'score': 2, 'reason': 'x'}", "malformed_json"),
    ('{"score": 2 "reason": "x"}', "malformed_json"),
    ('{"score": 2, "reason" "x"}', "malformed_json"),
    # nothing object-shaped at all
    ("The student scored 3 out of 5.", "not_json"),
    ("[1, 2, 3]", "not_json"),
    ("42", "not_json"),
    # decoded, but not the agreed shape or not an allowed mark
    ('{"reason": "x"}', "score_missing"),
    ('{"score": "excellent", "reason": "x"}', "score_not_numeric"),
    ('{"score": true, "reason": "x"}', "score_not_numeric"),
    ('{"score": -1, "reason": "x"}', "score_negative"),
    ('{"score": 99, "reason": "x"}', "score_above_max"),
    ('{"score": 3}', "reason_missing"),
    # nothing at all
    ("", "empty_response"),
    ("   ", "empty_response"),
    (None, "empty_response"),
])
def test_rejections_keep_their_code(raw, expected):
    assert _code(raw) == expected


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_scores_are_still_rejected_with_trailing_data(literal):
    raw = '{"score": %s, "reason": "x"} trailing' % literal
    assert _code(raw) == "score_not_finite"


def test_truncation_is_still_malformed_not_silently_recovered():
    """The boundary fix must not turn a cut-off body into a mark."""
    assert _code('{"score": 2, "reason": "The student correctly identified the') == "malformed_json"


# ---------------------------------------------------------------------------
# REJECTED -- ambiguity, the one genuinely NEW refusal
# ---------------------------------------------------------------------------

def test_two_consecutive_objects_are_ambiguous():
    assert _code(OBJECT + '{"score": 5, "reason": "different"}') == "ambiguous_json"


def test_a_second_object_after_prose_is_ambiguous():
    raw = OBJECT + "\nOn reflection:\n" + '{"score": 1, "reason": "revised"}'
    assert _code(raw) == "ambiguous_json"


def test_a_json_array_of_two_grading_objects_is_ambiguous():
    raw = '[{"score": 1, "reason": "a"}, {"score": 4, "reason": "b"}]'
    assert _code(raw) == "ambiguous_json"


def test_a_second_object_is_refused_even_when_identical():
    """Two payloads is ambiguity regardless of agreement; no guessing either way."""
    assert _code(OBJECT + " " + OBJECT) == "ambiguous_json"


def test_a_broken_second_object_is_harmless():
    """Only a DECODABLE second payload is ambiguous; broken trailing text is not."""
    assert parse_grading_response(OBJECT + ' {"score": ', max_marks=MAX).score == 3.0


# ---------------------------------------------------------------------------
# nothing leaks, and every code can be explained
# ---------------------------------------------------------------------------

def test_the_ambiguity_code_has_a_professor_facing_sentence():
    assert "ambiguous_json" in FAILURE_MESSAGES
    assert describe("ambiguous_json") == "The grading response contained more than one result."


def test_no_failure_message_quotes_the_provider_body():
    marker = "SENSITIVE-STUDENT-TEXT"
    raw = '{"score": 2, "reason": "%s",}' % marker
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response(raw, max_marks=MAX)
    assert marker not in describe(exc.value.code)
    assert marker not in FAILURE_MESSAGES[exc.value.code]


def test_the_decoder_never_reads_a_score_out_of_prose():
    """No regex score extraction: prose that mentions a number is still a failure."""
    for raw in ("The student scored 3 out of 5.", "Grade: 3\nReason: prose", "score = 3"):
        assert _code(raw) in ("not_json", "malformed_json")


def test_the_module_imports_no_provider_sdk():
    """Provider neutrality: the boundary fix is standard-library JSON only."""
    import backend.grading.result as result_module

    with open(result_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for vendor in ("google.generativeai", "openai", "anthropic", "gemini"):
        assert vendor not in source, f"{vendor} must not appear in the grading decoder"
    assert isinstance(result_module._DECODER, json.JSONDecoder)
