"""Automatic answer-script preparation: the stage that was missing.

An uploaded script and a set of questions existed; nothing turned the first
into per-question content. `enqueue_processing` therefore refused every run
("no prepared responses to grade yet") and the only way forward was a student
cutting their script up in the crop editor -- a human-first workflow in the
middle of an AI-first product.

These tests hold the contract of the stage that closes that gap, and they hold
the invariants it must not break on the way: an unprepared paper must never
aggregate to a fabricated zero, a model must never invent a question, and an
existing response -- crop-built, teacher-corrected or already graded -- must
never be overwritten.

No network, no key, no quota: the provider is a stub at the same seam the
offline pipeline harness uses.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.ai.answer_mapping import AnswerMappingError, parse_answer_mapping
from backend.ai.contracts import AITask
from backend.grading import preparation
from backend.grading.preparation import (
    ALREADY_PREPARED,
    MAPPING_INVALID,
    MAPPING_UNAVAILABLE,
    NO_ANSWER_SCRIPT,
    NO_ANSWERS_MAPPED,
    PREPARED,
    SCRIPT_UNREADABLE,
    prepare_student_responses,
)
from backend.models.files import AnswerScript
from backend.models.tables import Exam, Question, QuestionResponse

#: The reduced live test paper's shape.
CANONICAL = (31, 32, 36, 37, 39)


# ---------------------------------------------------------------------------
# the deterministic gate, with no database in sight
# ---------------------------------------------------------------------------

def _mapping(**entries):
    return json.dumps({"answers": [
        {"question_number": n, "answer": t} for n, t in entries.items()
    ]})


def test_a_valid_mapping_comes_through_in_question_order():
    raw = json.dumps({"answers": [
        {"question_number": 37, "answer": "later"},
        {"question_number": 31, "answer": "earlier"},
    ]})
    mapping = parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert [a.question_number for a in mapping.answers] == [31, 37]
    assert mapping.answers[0].answer_text == "earlier"


@pytest.mark.parametrize("shape", [31, "31", "Q31", "Question 31", 31.0])
def test_a_question_number_is_read_tolerantly(shape):
    """Tolerant about shape, strict about identity."""
    raw = json.dumps({"answers": [{"question_number": shape, "answer": "x"}]})
    mapping = parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert [a.question_number for a in mapping.answers] == [31]


@pytest.mark.parametrize("unknown", [33, 38, 1, 999])
def test_a_question_this_exam_does_not_have_is_discarded(unknown):
    """33 and 38 are the exact numbers the hidden-PDF-text bug produced."""
    raw = json.dumps({"answers": [
        {"question_number": 31, "answer": "kept"},
        {"question_number": unknown, "answer": "must not survive"},
    ]})
    mapping = parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert [a.question_number for a in mapping.answers] == [31]
    assert mapping.rejected_numbers == (unknown,)


def test_an_empty_answer_is_dropped_not_stored():
    """"Attempted but wrote nothing" is not something a mapping pass can assert."""
    raw = json.dumps({"answers": [
        {"question_number": 31, "answer": "   "},
        {"question_number": 32, "answer": None},
        {"question_number": 36, "answer": "real"},
    ]})
    mapping = parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert [a.question_number for a in mapping.answers] == [36]
    assert mapping.empty_numbers == (31, 32)


def test_a_duplicated_question_keeps_the_first_entry():
    raw = json.dumps({"answers": [
        {"question_number": 31, "answer": "first"},
        {"question_number": 31, "answer": "second"},
    ]})
    mapping = parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert len(mapping.answers) == 1
    assert mapping.answers[0].answer_text == "first"
    assert mapping.duplicate_numbers == (31,)


def test_an_empty_answer_list_is_a_valid_response_not_a_parse_error():
    """A blank script is a real outcome; the CALLER decides what it means."""
    mapping = parse_answer_mapping('{"answers": []}', allowed_numbers=CANONICAL)
    assert mapping.has_answers is False


def test_a_fenced_json_block_is_tolerated():
    raw = '```json\n{"answers":[{"question_number":31,"answer":"x"}]}\n```'
    assert parse_answer_mapping(raw, allowed_numbers=CANONICAL).has_answers


@pytest.mark.parametrize("raw,code", [
    (None, "empty_response"),
    ("   ", "empty_response"),
    ("not json at all", "not_json"),
    ("{broken", "malformed_json"),
    ('{"answers": "text"}', "wrong_schema"),
    ('{"other": []}', "answers_missing"),
    ('[1,2,3]', "not_json"),
])
def test_an_unusable_response_raises_a_named_code(raw, code):
    with pytest.raises(AnswerMappingError) as exc:
        parse_answer_mapping(raw, allowed_numbers=CANONICAL)
    assert exc.value.code == code


def test_the_error_never_carries_the_model_text_into_its_message():
    with pytest.raises(AnswerMappingError) as exc:
        parse_answer_mapping("the student wrote SECRET-ANSWER", allowed_numbers=CANONICAL)
    assert "SECRET-ANSWER" not in exc.value.message


# ---------------------------------------------------------------------------
# the stage itself
# ---------------------------------------------------------------------------

class _MappingStub:
    """Stands in for `ai_services.map_answer_script`. Records what it was asked."""

    def __init__(self, *, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = []

    async def __call__(self, script_path, *, question_numbers, exam_id=None, student_id=None):
        self.calls.append({
            "script_path": script_path,
            "question_numbers": list(question_numbers),
            "exam_id": exam_id,
            "student_id": student_id,
        })
        if self.error is not None:
            raise self.error
        return self.raw


@pytest_asyncio.fixture
async def paper(db, world, tmp_path):
    """One exam shaped like the live test paper, with a script and no responses."""
    exam = Exam(title="Prep Exam", classroom_id=world["class_a"].id,
                author_id=world["owner_prof"].id, exam_stage=5)
    db.add(exam)
    await db.commit()
    await db.refresh(exam)

    questions = {}
    for number in CANONICAL:
        q = Question(exam_id=exam.id, question_number=number,
                     text=f"Question {number}", max_marks=4)
        db.add(q)
        questions[number] = q
    await db.commit()
    for q in questions.values():
        await db.refresh(q)

    script_path = tmp_path / "script.png"
    script_path.write_bytes(b"\x89PNG\r\n\x1a\n synthetic answer script")
    script = AnswerScript(title="script.png", file_path=str(script_path),
                          exam_id=exam.id, student_id=world["student_a"].id)
    db.add(script)
    await db.commit()
    await db.refresh(script)

    return {"exam": exam, "questions": questions, "script": script,
            "student": world["student_a"]}


async def _responses(db, paper):
    db.expunge_all()
    rows = (await db.execute(
        select(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == paper["exam"].id)
        .order_by(Question.question_number)
    )).scalars().all()
    return rows


async def _numbers(db, paper):
    by_id = {q.id: n for n, q in paper["questions"].items()}
    return sorted(by_id[r.question_id] for r in await _responses(db, paper))


@pytest.mark.asyncio
async def test_preparation_creates_a_response_per_mapped_question(db, paper, monkeypatch):
    stub = _MappingStub(raw=_mapping(**{"31": "a31", "36": "a36", "39": "a39"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == PREPARED
    assert outcome.created == 3
    assert outcome.ready is True
    assert await _numbers(db, paper) == [31, 36, 39]
    texts = {r.answer_text for r in await _responses(db, paper)}
    assert texts == {"a31", "a36", "a39"}


@pytest.mark.asyncio
async def test_the_whole_script_is_read_in_one_call_with_the_canonical_numbers(
    db, paper, monkeypatch
):
    """One request for the paper, not one per question -- quota is scarce."""
    stub = _MappingStub(raw=_mapping(**{"31": "a"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert len(stub.calls) == 1, "preparation must not call once per question"
    assert stub.calls[0]["question_numbers"] == list(CANONICAL)
    assert stub.calls[0]["script_path"] == paper["script"].file_path


@pytest.mark.asyncio
async def test_a_question_the_exam_does_not_have_creates_nothing(db, paper, monkeypatch):
    """The model naming 33 or 38 must not resurrect them."""
    stub = _MappingStub(raw=json.dumps({"answers": [
        {"question_number": 31, "answer": "real"},
        {"question_number": 33, "answer": "phantom"},
        {"question_number": 38, "answer": "phantom"},
    ]}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.created == 1
    assert outcome.rejected_numbers == (33, 38)
    assert await _numbers(db, paper) == [31]

    db.expunge_all()
    numbers = (await db.execute(
        select(Question.question_number).where(Question.exam_id == paper["exam"].id)
    )).scalars().all()
    assert sorted(numbers) == list(CANONICAL), "a Question row was invented"


@pytest.mark.asyncio
async def test_zero_valid_answers_prepares_nothing_and_is_not_ready(db, paper, monkeypatch):
    """No fake rows to satisfy a gate: that only moves the vacuous-zero bug."""
    stub = _MappingStub(raw='{"answers": []}')
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == NO_ANSWERS_MAPPED
    assert outcome.ready is False
    assert await _responses(db, paper) == []


@pytest.mark.asyncio
async def test_an_existing_response_is_never_overwritten(db, paper, monkeypatch):
    """A crop-built, teacher-corrected or already-graded row must survive."""
    existing = QuestionResponse(
        question_id=paper["questions"][31].id, student_id=paper["student"].id,
        answer_text="the student's own prepared text", marks_obtained=2.5,
        ans_text_images=json.dumps(["/some/legacy/crop.png"]),
    )
    db.add(existing)
    await db.commit()

    stub = _MappingStub(raw=_mapping(**{"31": "OVERWRITE ME", "32": "new"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == ALREADY_PREPARED
    assert outcome.created == 0
    assert outcome.ready is True, "an already-prepared paper is still gradeable"
    assert stub.calls == [], "no provider call for an already-prepared paper"

    rows = await _responses(db, paper)
    assert len(rows) == 1, "preparation added a duplicate row"
    assert rows[0].answer_text == "the student's own prepared text"
    assert rows[0].marks_obtained == 2.5
    assert json.loads(rows[0].ans_text_images) == ["/some/legacy/crop.png"]


@pytest.mark.asyncio
async def test_running_preparation_twice_creates_no_duplicates(db, paper, monkeypatch):
    """There is no unique (student, question) constraint to catch this."""
    stub = _MappingStub(raw=_mapping(**{"31": "a", "32": "b"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    await prepare_student_responses(paper["exam"].id, paper["student"].id, db)
    second = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert second.status == ALREADY_PREPARED
    assert len(await _responses(db, paper)) == 2
    assert await _numbers(db, paper) == [31, 32]
    assert len(stub.calls) == 1, "the second run called the provider again"


@pytest.mark.asyncio
async def test_no_answer_script_is_refused_before_any_provider_call(db, paper, monkeypatch):
    stub = _MappingStub(raw=_mapping(**{"31": "a"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)
    await db.delete(paper["script"])
    await db.commit()

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == NO_ANSWER_SCRIPT
    assert outcome.ready is False
    assert stub.calls == []
    assert await _responses(db, paper) == []


@pytest.mark.asyncio
async def test_a_provider_failure_writes_no_partial_responses(db, paper, monkeypatch):
    from backend.ai.errors import ProviderTemporaryError

    stub = _MappingStub(error=ProviderTemporaryError("upstream is unwell"))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == MAPPING_UNAVAILABLE
    assert outcome.ready is False
    assert await _responses(db, paper) == []


@pytest.mark.asyncio
async def test_an_unusable_response_writes_no_partial_responses(db, paper, monkeypatch):
    stub = _MappingStub(raw="the model wrote prose instead of JSON")
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == MAPPING_INVALID
    assert await _responses(db, paper) == []


@pytest.mark.asyncio
async def test_an_unrenderable_script_is_named_not_retried_raw(db, paper, monkeypatch):
    """Falling back to the raw file is the hidden-PDF-text bug all over again."""
    from backend.ai.documents import DocumentNormalisationError

    stub = _MappingStub(error=DocumentNormalisationError("document_unreadable", "nope"))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    outcome = await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    assert outcome.status == SCRIPT_UNREADABLE
    assert await _responses(db, paper) == []


@pytest.mark.asyncio
async def test_preparation_never_writes_to_the_reference_side(db, paper, monkeypatch):
    """Student evidence must not land in the marking-scheme slot (audit C1)."""
    stub = _MappingStub(raw=_mapping(**{"31": "STUDENT-TEXT", "32": "STUDENT-TEXT-2"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    db.expunge_all()
    questions = (await db.execute(
        select(Question).where(Question.exam_id == paper["exam"].id)
    )).scalars().all()
    for q in questions:
        assert q.ideal_marking_scheme is None
        assert q.ideal_answer is None
        for slot in (q.ms_text_images, q.ms_table_images, q.ms_diagram_images):
            assert not slot, "student evidence reached a reference slot"


@pytest.mark.asyncio
async def test_preparation_logs_no_student_answer_text(db, paper, monkeypatch, caplog):
    import logging

    stub = _MappingStub(raw=_mapping(**{"31": "SYNTHETIC-STUDENT-ANSWER-BODY"}))
    monkeypatch.setattr(preparation.ai_services, "map_answer_script", stub)

    # Scoped to the `backend` tree, as `test_offline_run_logs_no_student_or_
    # scheme_content` is: a DB driver's DEBUG statement echo is a logging-level
    # decision, not something this code controls.
    caplog.set_level(logging.DEBUG, logger="backend")
    await prepare_student_responses(paper["exam"].id, paper["student"].id, db)

    ours = [r for r in caplog.records if r.name.startswith("backend")]
    assert ours, "preparation logged nothing at all"
    text = "\n".join(r.getMessage() for r in ours)
    assert "SYNTHETIC-STUDENT-ANSWER-BODY" not in text


# ---------------------------------------------------------------------------
# the service: one call, and the VISIBLE document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_mapping_request_uses_the_visible_document(tmp_path, monkeypatch):
    """A PDF must reach the provider as pages, never as a file with a text layer."""
    pytest.importorskip("pypdfium2")

    from backend.ai import providers, services
    from backend.tests.test_document_visibility import paper_with_hidden_question

    class _Recorder:
        name = "gemini"

        def __init__(self):
            self.media = []
            self.prompts = []

        async def run_text_task(self, request, settings, *, timeout_seconds=None):
            from backend.ai.contracts import ProviderResponse, TextPart

            for part in request.parts:
                if isinstance(part, TextPart):
                    self.prompts.append(part.text)
            for path in request.file_paths:
                with open(path, "rb") as handle:
                    self.media.append({"suffix": path.lower().rsplit(".", 1)[-1],
                                       "head": handle.read(8)})
            return ProviderResponse(
                text='{"answers": []}', provider=self.name, model="test",
                task=request.task, prompt_version=request.prompt_version,
                attempts=1, duration_ms=0, uploaded_file_count=len(request.file_paths),
                warnings=(),
            )

    recorder = _Recorder()
    providers.register_provider(recorder.name, recorder)
    try:
        script = paper_with_hidden_question(tmp_path / "script.pdf")
        await services.map_answer_script(script, question_numbers=CANONICAL, exam_id=1)
    finally:
        providers.reset_providers()

    assert recorder.media, "nothing was sent"
    for item in recorder.media:
        assert item["suffix"] == "png", "a PDF reached the provider"
        assert item["head"] == b"\x89PNG\r\n\x1a\n"
    # The canonical numbers are stated in the prompt as well as enforced after.
    assert "31, 32, 36, 37, 39" in recorder.prompts[0]


def test_the_mapping_task_is_registered_and_asks_for_json():
    from backend.ai.config import get_task_settings

    assert AITask.ANSWER_MAPPING in AITask.ALL
    settings = get_task_settings(AITask.ANSWER_MAPPING)
    assert settings.expects_json is True
    assert settings.max_concurrency == 1, "one call reads the whole script"


def test_the_task_name_is_a_capability_not_a_vendor():
    """Provider neutrality: nothing here may be named for whoever runs it."""
    import pathlib

    from backend.tests.conftest import REPO_ROOT

    for name in ("backend/ai/answer_mapping.py", "backend/grading/preparation.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        lowered = source.lower()
        assert "gemini" not in lowered, f"{name} names a provider"
        assert "genai" not in lowered
        assert "google" not in lowered
