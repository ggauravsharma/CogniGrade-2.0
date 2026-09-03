"""What `malformed_json` actually means, and what the provider stopped telling us.

Q31 of the live exam-4 run recorded `grading_error_code = malformed_json` after
three attempts. Reading the run's telemetry rather than assuming:

    12:53:01  provider error, retrying: task=grading category=timeout attempt=1/3
    12:55:02  provider error, retrying: task=grading category=timeout attempt=2/3
    12:56:38  ai_invocation task=grading ... attempts=3 success=True

Attempts 1 and 2 were TIMEOUTS, not malformed responses. Only attempt 3 came
back with a body, and that body did not decode. So the earlier summary -- "all
three attempts failed with malformed_json" -- was wrong, and the interesting
question is what attempt 3 actually returned.

The adapter could not say. `response.text` returns the PARTIAL body when
generation stopped at an output limit, so a truncated answer is indistinguishable
from a badly-written one by the time it reaches the decoder. The provider knew;
the adapter dropped it.

This file pins both halves:

  * the decoder's exact behaviour, so `malformed_json` has one meaning and the
    other failure codes keep theirs -- characterisation, NOT a licence to make
    the parser permissive;
  * the finish reason and token counts now carried out of the adapter, so the
    next failure of this shape can be diagnosed without logging one character
    of a student's answer.

No network, no provider, no quota.
"""

from __future__ import annotations

import json
import math

import pytest

from backend.ai.contracts import FinishReason, ProviderResponse
from backend.grading.failure import FAILURE_MESSAGES, describe
from backend.grading.result import GradingResponseError, parse_grading_response


def _code(raw, *, max_marks=5):
    with pytest.raises(GradingResponseError) as exc:
        parse_grading_response(raw, max_marks=max_marks)
    return exc.value.code


# ---------------------------------------------------------------------------
# what the decoder ACCEPTS
# ---------------------------------------------------------------------------

def test_exact_json_is_accepted():
    result = parse_grading_response('{"score": 3, "reason": "ok"}', max_marks=5)
    assert result.score == 3.0
    assert result.reason == "ok"


def test_surrounding_whitespace_is_accepted():
    assert parse_grading_response('\n\n  {"score": 2, "reason": "x"}  \n', max_marks=5).score == 2.0


def test_a_fenced_block_is_accepted():
    raw = '```json\n{"score": 1.5, "reason": "half"}\n```'
    assert parse_grading_response(raw, max_marks=5).score == 1.5


def test_prose_before_the_json_is_accepted():
    raw = 'Here is my assessment.\n{"score": 4, "reason": "good"}'
    assert parse_grading_response(raw, max_marks=5).score == 4.0


def test_unexpected_extra_fields_are_ignored():
    raw = '{"score": 2, "reason": "x", "confidence": 0.9, "rubric": ["a"]}'
    assert parse_grading_response(raw, max_marks=5).score == 2.0


def test_a_numeric_string_score_is_accepted():
    """The model writing "2.5" instead of 2.5 must not lose the student a mark."""
    assert parse_grading_response('{"score": "2.5", "reason": "x"}', max_marks=5).score == 2.5


def test_a_genuine_zero_is_a_score_not_a_failure():
    result = parse_grading_response('{"score": 0, "reason": "nothing correct"}', max_marks=5)
    assert result.score == 0.0, "a real zero must survive the decoder"


def test_a_fractional_score_survives():
    assert parse_grading_response('{"score": 3.25, "reason": "x"}', max_marks=5).score == 3.25


# ---------------------------------------------------------------------------
# what the decoder REJECTS, and under which code
# ---------------------------------------------------------------------------
#
# Each of these is a DELIBERATE rejection. The point of pinning them is that
# `malformed_json` should mean one specific thing -- json.loads failed on text
# that looked like an object -- and not quietly absorb schema or range problems
# that have their own codes and their own sentences.

@pytest.mark.parametrize("raw,expected", [
    # json.loads failed on something that started like an object
    ('{"score": 2, "reason": "unterminated', "malformed_json"),
    ('{"score": 2, "reason": "x",}', "malformed_json"),
    ("{'score': 2, 'reason': 'x'}", "malformed_json"),
    ('{"score": 2 "reason": "x"}', "malformed_json"),
    # nothing object-shaped at all
    ("The student scored 3 out of 5.", "not_json"),
    ("[1, 2, 3]", "not_json"),
    # decoded, but not the agreed shape
    ('{"reason": "x"}', "score_missing"),
    ('{"score": "excellent", "reason": "x"}', "score_not_numeric"),
    ('{"score": true, "reason": "x"}', "score_not_numeric"),
    ('{"score": null, "reason": "x"}', "score_missing"),
    ('{"score": -1, "reason": "x"}', "score_negative"),
    ('{"score": 99, "reason": "x"}', "score_above_max"),
    # nothing at all
    ("", "empty_response"),
    ("   ", "empty_response"),
    (None, "empty_response"),
])
def test_each_rejection_keeps_its_own_code(raw, expected):
    assert _code(raw) == expected


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_scores_are_rejected(literal):
    """`json.loads` accepts these; a mark must not be one of them."""
    assert _code('{"score": %s, "reason": "x"}' % literal) == "score_not_finite"


def test_a_truncated_response_is_indistinguishable_from_bad_json_to_the_decoder():
    """The exact shape Q31 would produce if it were cut off, and why telemetry matters.

    A response stopped at an output limit still returns its partial text, so it
    arrives here as an unterminated object and gets `malformed_json` -- the same
    code a model writing nonsense would get. The decoder cannot and should not
    guess; the provider's finish reason is the only thing that separates them,
    which is what the adapter now carries.
    """
    truncated = '{"score": 2, "reason": "The student correctly identified the'
    assert _code(truncated) == "malformed_json"

    # WAS pinned here as `malformed_json` too, and that was the defect: a
    # COMPLETE object with trailing material is not a truncated one, and the
    # live Q31 body was exactly this shape. It is now accepted -- see
    # `test_trailing_data.py`. Truncation above still fails, which is the
    # distinction this test exists to hold.
    complete_with_trailing = '{"score": 2, "reason": "complete", "extra": "junk"} garbage'
    assert parse_grading_response(complete_with_trailing, max_marks=5).score == 2.0


def test_every_code_the_decoder_raises_has_a_safe_sentence():
    """A code with no message degrades to a generic line; that is a real gap."""
    raised = set()
    for raw in ('{"score": 2, "reason": "x",}', "prose only", '{"reason": "x"}',
                '{"score": "abc", "reason": "x"}', '{"score": -1, "reason": "x"}',
                '{"score": 99, "reason": "x"}', '{"score": NaN, "reason": "x"}', ""):
        raised.add(_code(raw))

    missing = sorted(c for c in raised if c not in FAILURE_MESSAGES)
    assert missing == ["not_json"], (
        f"unexpected codes without a professor-facing sentence: {missing}"
    )
    # Documented, not silently wrong: `not_json` degrades to the generic line.
    assert describe("not_json") == "Grading did not produce a valid result."


# ---------------------------------------------------------------------------
# the finish reason the adapter used to discard
# ---------------------------------------------------------------------------

class _Candidate:
    def __init__(self, reason):
        self.finish_reason = reason


class _Reason:
    """Stands in for the SDK enum, which exposes `.name`."""

    def __init__(self, name):
        self.name = name


class _Usage:
    def __init__(self, prompt, output):
        self.prompt_token_count = prompt
        self.candidates_token_count = output


class _Response:
    def __init__(self, *, reason=None, usage=None, candidates=None):
        self.candidates = candidates if candidates is not None else (
            [_Candidate(reason)] if reason is not None else []
        )
        self.usage_metadata = usage


@pytest.mark.parametrize("sdk_name,expected", [
    ("STOP", FinishReason.COMPLETE),
    ("MAX_TOKENS", FinishReason.TRUNCATED),
    ("SAFETY", FinishReason.BLOCKED),
    ("RECITATION", FinishReason.BLOCKED),
    ("BLOCKLIST", FinishReason.BLOCKED),
    ("PROHIBITED_CONTENT", FinishReason.BLOCKED),
    ("SPII", FinishReason.BLOCKED),
    ("FINISH_REASON_UNSPECIFIED", FinishReason.UNKNOWN),
    ("MALFORMED_FUNCTION_CALL", FinishReason.OTHER),
    ("LANGUAGE", FinishReason.OTHER),
])
def test_the_adapter_translates_the_vendor_reason(sdk_name, expected):
    """Layer 3 speaks the vendor enum; nothing above it does."""
    from backend.ai.providers.gemini import GeminiProvider

    assert GeminiProvider._finish_reason(_Response(reason=_Reason(sdk_name))) == expected


@pytest.mark.parametrize("response", [
    _Response(candidates=[]),
    _Response(candidates=[_Candidate(None)]),
    object(),
])
def test_an_unreadable_finish_reason_is_unknown_never_an_exception(response):
    """A diagnostic must never take down a grading call that otherwise worked."""
    from backend.ai.providers.gemini import GeminiProvider

    assert GeminiProvider._finish_reason(response) == FinishReason.UNKNOWN


def test_token_counts_are_read_when_present():
    from backend.ai.providers.gemini import GeminiProvider

    assert GeminiProvider._token_counts(_Response(usage=_Usage(1200, 480))) == (1200, 480)


@pytest.mark.parametrize("response", [_Response(usage=None), object()])
def test_missing_token_counts_are_none_never_an_exception(response):
    from backend.ai.providers.gemini import GeminiProvider

    assert GeminiProvider._token_counts(response) == (None, None)


def test_the_response_contract_carries_the_reason_provider_neutrally():
    """Generic field, generic vocabulary: no vendor spelling above the adapter."""
    response = ProviderResponse(
        text="{}", provider="anything", model="anything", task="grading",
        prompt_version="grading/v1", finish_reason=FinishReason.TRUNCATED,
        input_tokens=10, output_tokens=2048,
    )
    assert response.finish_reason == "truncated"
    assert response.output_tokens == 2048
    # Defaults keep every existing construction site valid.
    bare = ProviderResponse(text="", provider="p", model="m", task="grading",
                            prompt_version="v")
    assert bare.finish_reason is None and bare.output_tokens is None


def test_no_vendor_finish_reason_name_escapes_the_adapter():
    import pathlib

    from backend.tests.conftest import REPO_ROOT

    vendor_names = ("MAX_TOKENS", "RECITATION", "PROHIBITED_CONTENT", "SPII", "BLOCKLIST")
    for path in (REPO_ROOT / "backend").rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "old" in parts or "migrations" in parts:
            continue
        if path.name == "gemini.py":
            continue  # the one module allowed to speak the vendor's enum
        source = path.read_text(encoding="utf-8", errors="replace")
        for name in vendor_names:
            assert name not in source, f"{path.name} names a vendor finish reason"


@pytest.mark.asyncio
async def test_a_truncated_response_is_flagged_and_logged_without_its_body(caplog):
    """End to end through the real adapter, with the SDK call stubbed."""
    import logging

    from backend.ai.config import get_task_settings
    from backend.ai.contracts import AITask, ProviderRequest
    from backend.ai.providers.gemini import GeminiProvider

    body = '{"score": 2, "reason": "SYNTHETIC-ANSWER-BODY-that-was-cut'

    class _Stub(_Response):
        text = body

    provider = GeminiProvider(api_keys=["not-used-no-call-is-made"])
    provider._model_for = lambda name: object()
    provider._call_model = lambda model, contents, config, budget: _Stub(
        reason=_Reason("MAX_TOKENS"), usage=_Usage(900, 2048)
    )

    request = ProviderRequest.simple(
        task=AITask.GRADING, prompt="grade this", prompt_version="grading/v1"
    )
    caplog.set_level(logging.WARNING, logger="backend")
    response = await provider.run_text_task(request, get_task_settings(AITask.GRADING))

    assert response.finish_reason == FinishReason.TRUNCATED
    assert response.output_tokens == 2048
    assert "truncated_response" in response.warnings

    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("backend"))
    assert "truncated" in logged.lower(), "the truncation was not reported at all"
    assert "SYNTHETIC-ANSWER-BODY" not in logged, "the response body was logged"

    # And the decoder still refuses it, exactly as before.
    assert _code(response.text) == "malformed_json"


def test_json_mode_is_actually_requested_of_the_provider():
    """`expects_json` is a provider-level mode here, not just a prompt wish."""
    from backend.ai.config import get_task_settings
    from backend.ai.contracts import AITask, ProviderRequest
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(api_keys=["not-used"])
    settings = get_task_settings(AITask.GRADING)
    request = ProviderRequest.simple(task=AITask.GRADING, prompt="x", prompt_version="v")

    config = provider._generation_config(settings, request)
    assert config["response_mime_type"] == "application/json"
    assert config["temperature"] == 0.0
