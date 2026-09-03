"""The AI-first path end to end, with nobody cutting anything up.

`test_offline_pipeline.py` proves the pipeline from ALREADY-PREPARED responses
onward. This proves the step before it: a student uploads a script, presses
nothing else, and the same Celery task body produces marks.

    tasks._process_and_grade
      -> grading.preparation.prepare_student_responses      <- the new stage
           -> ai_services.map_answer_script                 (visible pages)
                -> ai.services.run_task -> PROVIDER
           -> ai.answer_mapping.parse_answer_mapping        (canonical gate)
           -> QuestionResponse rows
      -> geminiAPI.process_answer_text_images_logic          no crops: no-op
      -> geminiAPI.grade_exam_logic                          unchanged
      -> examStats.add_exam_result_internal                  unchanged
      -> exams.set_exam_stage                                unchanged

The stub sits at the `TextTaskProvider` seam, so prompt assembly, the visible-
document boundary, validation, persistence, aggregation and the stage
transition are all production code. NO manual crop route is called, and no
`ans_*_images` value is ever written -- that is the point of the file.

NO LIVE PROVIDER -- zero API quota.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.ai.contracts import AITask, TextPart
from backend.grading.aggregation import ExamResultStatus
from backend.models.files import AnswerScript
from backend.models.tables import Exam, ExamResult, Question, QuestionResponse
from backend.routers.exams import EXAM_STAGE_GRADED, EXAM_STAGE_GRADING
from backend.tests.test_offline_pipeline import session_factory_of

#: The reduced live paper's shape, and one number the model must never invent.
CANONICAL = (31, 32, 36, 37, 39)
PHANTOM = 33

MAX_MARKS = {31: 3.0, 32: 3.0, 36: 5.0, 37: 4.0, 39: 4.0}

#: What the mapping stub says the student wrote. 39 is deliberately ABSENT:
#: the student did not attempt it, and that must stay distinguishable from a
#: zero.
MAPPED_ANSWERS = {
    31: "SYNTHETIC-ANSWER-31 the lens is concave",
    32: "SYNTHETIC-ANSWER-32 it becomes an electromagnet",
    36: "SYNTHETIC-ANSWER-36 working shown",
    37: "SYNTHETIC-ANSWER-37 i do not know",
}

#: 36 is fractional, 37 is a genuine earned zero.
GRADES = {31: 2.0, 32: 3.0, 36: 3.5, 37: 0.0}


class AutoPipelineProvider:
    """Answers ANSWER_MAPPING and GRADING. Fails loudly on anything else."""

    name = "gemini"

    def __init__(self, *, mapping_body=None, grades=None, mapping_error=None):
        self.mapping_body = mapping_body
        self.mapping_error = mapping_error
        self.grades = dict(grades if grades is not None else GRADES)
        self.mapping_calls = []
        self.grading_calls = []
        self.recognition_calls = []

    async def run_text_task(self, request, settings, *, timeout_seconds=None):
        from backend.ai.contracts import ProviderResponse

        if request.task == AITask.ANSWER_MAPPING:
            body = self._map(request)
        elif request.task == AITask.GRADING:
            body = self._grade(request)
        elif request.task == AITask.ANSWER_RECOGNITION:
            # Must never happen on this path: there are no crops to recognise.
            self.recognition_calls.append(list(request.file_paths))
            raise AssertionError("the automatic path called crop recognition")
        else:  # pragma: no cover
            raise AssertionError(f"unexpected task {request.task!r}")

        return ProviderResponse(
            text=body, provider=self.name, model=settings.model,
            task=request.task, prompt_version=request.prompt_version,
        )

    def _map(self, request) -> str:
        self.mapping_calls.append({
            "file_paths": list(request.file_paths),
            "prompt": "\n".join(
                p.text for p in request.parts if isinstance(p, TextPart)
            ),
        })
        if self.mapping_error is not None:
            raise self.mapping_error
        if self.mapping_body is not None:
            return self.mapping_body
        return json.dumps({"answers": [
            {"question_number": n, "answer": t} for n, t in MAPPED_ANSWERS.items()
        ]})

    def _grade(self, request) -> str:
        joined = "\n".join(p.text for p in request.parts if isinstance(p, TextPart))
        number = next((n for n in CANONICAL if f"CG-MARK-{n}" in joined), None)
        assert number is not None, "the grading prompt named no known question"
        self.grading_calls.append({"question_number": number, "prompt": joined})
        outcome = self.grades.get(number)
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome is not None, f"no grade configured for {number}"
        return json.dumps({"score": outcome, "reason": f"synthetic reason {number}"})

    @property
    def graded_numbers(self):
        return sorted(c["question_number"] for c in self.grading_calls)


@pytest_asyncio.fixture
async def auto(db, world, tmp_path, monkeypatch):
    """An exam with a script and NO responses: the state exam 4 was in."""
    import backend.tasks as tasks
    from backend.ai import providers

    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "1")
    monkeypatch.setenv("CG_AI__GRADING__MAX_RETRIES", "0")
    monkeypatch.setenv("CG_AI__ANSWER_MAPPING__MAX_RETRIES", "0")

    student = world["student_a"]
    exam = Exam(title="Auto Pipeline Exam", classroom_id=world["class_a"].id,
                author_id=world["owner_prof"].id, exam_stage=5)
    db.add(exam)
    await db.commit()
    await db.refresh(exam)

    questions = {}
    for number in CANONICAL:
        q = Question(
            exam_id=exam.id, question_number=number,
            # The marker the grading stub reads out of the REAL prompt.
            text=f"CG-MARK-{number} question {number}",
            max_marks=MAX_MARKS[number],
            ideal_marking_scheme=f"SYNTHETIC-SCHEME-{number} award per point",
        )
        db.add(q)
        questions[number] = q
    await db.commit()
    for q in questions.values():
        await db.refresh(q)

    # A raster script, so the renderer needs no PDF backend here. The PDF
    # visible-page path has its own coverage in test_answer_preparation.py.
    from PIL import Image

    script_path = tmp_path / "auto-script.png"
    Image.new("RGB", (200, 280), (250, 250, 250)).save(script_path, "PNG")
    script = AnswerScript(title="auto-script.png", file_path=str(script_path),
                          exam_id=exam.id, student_id=student.id)
    db.add(script)
    await db.commit()
    await db.refresh(script)

    state = {"exam": exam, "questions": questions, "student": student,
             "script": script, "provider": None}

    async def run(**kwargs):
        provider = AutoPipelineProvider(**kwargs)
        providers.register_provider(provider.name, provider)
        monkeypatch.setattr(tasks, "AsyncSessionLocal", session_factory_of(db))
        await db.commit()
        db.expunge_all()
        await tasks._process_and_grade(exam.id, student.id)
        db.expunge_all()
        state["provider"] = provider
        return provider

    state["run"] = run
    yield state
    providers.reset_providers()


async def _responses_by_number(db, auto):
    db.expunge_all()
    rows = (await db.execute(
        select(Question.question_number, QuestionResponse)
        .join(QuestionResponse, QuestionResponse.question_id == Question.id)
        .where(Question.exam_id == auto["exam"].id)
    )).all()
    return {number: response for number, response in rows}


async def _exam_result(db, auto):
    db.expunge_all()
    return (await db.execute(
        select(ExamResult).where(
            ExamResult.exam_id == auto["exam"].id,
            ExamResult.student_id == auto["student"].id,
        )
    )).scalars().first()


async def _stage(db, auto):
    db.expunge_all()
    return (await db.execute(
        select(Exam.exam_stage).where(Exam.id == auto["exam"].id)
    )).scalar()


# ---------------------------------------------------------------------------
# the whole automatic path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_uploaded_script_alone_reaches_marks(db, auto):
    """Upload -> prepare -> grade -> aggregate, with no human step in between."""
    provider = await auto["run"]()

    assert len(provider.mapping_calls) == 1, "the script must be read in one call"
    assert provider.recognition_calls == [], "crop recognition was invoked"

    responses = await _responses_by_number(db, auto)
    assert sorted(responses) == [31, 32, 36, 37], "unexpected responses created"
    assert 39 not in responses, "an unattempted question must not get a row"

    assert provider.graded_numbers == [31, 32, 36, 37]
    assert {n: float(r.marks_obtained) for n, r in responses.items()} == GRADES


@pytest.mark.asyncio
async def test_no_manual_crop_data_is_written_anywhere(db, auto):
    """The legacy crop columns must stay untouched by the automatic path."""
    await auto["run"]()

    for response in (await _responses_by_number(db, auto)).values():
        assert not response.ans_text_images
        assert not response.ans_table_images
        assert not response.ans_diagram_images
        assert response.answer_text, "the answer text is the evidence now"


@pytest.mark.asyncio
async def test_a_genuine_zero_and_a_fractional_mark_both_survive(db, auto):
    await auto["run"]()
    responses = await _responses_by_number(db, auto)

    assert responses[37].marks_obtained is not None, "a real zero became NULL"
    assert float(responses[37].marks_obtained) == 0.0, "a real zero was lost"
    assert responses[37].grading_error_code is None, "an earned zero looks like a failure"
    assert float(responses[36].marks_obtained) == 3.5, "fractional mark truncated"


@pytest.mark.asyncio
async def test_the_exam_finalises_because_every_prepared_answer_was_graded(db, auto):
    """A question the student never attempted must not block finalisation."""
    await auto["run"]()

    result = await _exam_result(db, auto)
    assert result is not None
    assert result.status == ExamResultStatus.GRADED
    assert result.graded_at is not None
    # 2.0 + 3.0 + 3.5 + 0.0
    assert float(result.marks_obtained) == 8.5
    assert await _stage(db, auto) == EXAM_STAGE_GRADED


@pytest.mark.asyncio
async def test_a_phantom_question_number_is_never_created(db, auto):
    """The model naming 33 must not resurrect the question the paper dropped."""
    body = json.dumps({"answers": [
        {"question_number": 31, "answer": "real"},
        {"question_number": PHANTOM, "answer": "phantom"},
    ]})
    await auto["run"](mapping_body=body, grades={31: 3.0})

    db.expunge_all()
    numbers = (await db.execute(
        select(Question.question_number).where(Question.exam_id == auto["exam"].id)
    )).scalars().all()
    assert sorted(numbers) == list(CANONICAL), "a Question row was invented"
    assert sorted(await _responses_by_number(db, auto)) == [31]


@pytest.mark.asyncio
async def test_a_failed_grade_stays_null_and_blocks_finalisation(db, auto):
    """Per-question isolation and C6, on the automatic path."""
    from backend.ai.errors import ProviderInvalidRequestError

    grades = dict(GRADES)
    grades[36] = ProviderInvalidRequestError("synthetic failure", provider="gemini")
    await auto["run"](grades=grades)

    responses = await _responses_by_number(db, auto)
    assert responses[36].marks_obtained is None, "a failure became a zero"
    assert responses[36].grading_error_code, "no reason was recorded"
    # Siblings unaffected.
    assert float(responses[31].marks_obtained) == 2.0
    assert float(responses[37].marks_obtained) == 0.0

    result = await _exam_result(db, auto)
    assert result.status != ExamResultStatus.GRADED
    assert result.graded_at is None
    assert await _stage(db, auto) == EXAM_STAGE_GRADING


# ---------------------------------------------------------------------------
# preparation failing must never look like a finished paper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_mapped_answers_produce_no_result_at_all(db, auto):
    """The vacuous-aggregation trap: zero rows must not become a final 0.0."""
    await auto["run"](mapping_body='{"answers": []}')

    assert await _responses_by_number(db, auto) == {}
    assert await _exam_result(db, auto) is None, "a fabricated result was written"
    assert await _stage(db, auto) == 5, "the stage moved on a paper never graded"


@pytest.mark.asyncio
async def test_a_mapping_provider_failure_produces_no_result(db, auto):
    from backend.ai.errors import ProviderTemporaryError

    await auto["run"](mapping_error=ProviderTemporaryError("upstream unwell"))

    assert await _responses_by_number(db, auto) == {}
    assert await _exam_result(db, auto) is None
    assert await _stage(db, auto) == 5


@pytest.mark.asyncio
async def test_rerunning_after_success_does_not_remap_or_duplicate(db, auto):
    """Preparation is a no-op once responses exist, whoever created them."""
    await auto["run"]()
    first = await _responses_by_number(db, auto)

    second = await auto["run"]()

    assert second.mapping_calls == [], "the script was mapped again"
    again = await _responses_by_number(db, auto)
    assert sorted(again) == sorted(first), "the response set changed"
    assert {n: float(r.marks_obtained) for n, r in again.items()} == GRADES

    db.expunge_all()
    total = (await db.execute(
        select(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == auto["exam"].id)
    )).scalars().all()
    assert len(total) == 4, "a duplicate response row was created"


@pytest.mark.asyncio
async def test_the_run_logs_no_student_answer_or_scheme_content(db, auto, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="backend")
    await auto["run"]()

    ours = [r for r in caplog.records if r.name.startswith("backend")]
    text = "\n".join(r.getMessage() for r in ours)
    for answer in MAPPED_ANSWERS.values():
        assert answer not in text, "a student's answer was logged"
    for number in CANONICAL:
        assert f"SYNTHETIC-SCHEME-{number}" not in text, "a marking scheme was logged"
