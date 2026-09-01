"""The whole automatic pipeline, offline, end to end.

Every other suite tests one stage in isolation. This one runs the REAL
orchestration a Celery worker runs, with nothing replaced except the vendor
call itself:

    tasks._process_and_grade                        the Celery task body
      -> geminiAPI.process_answer_text_images_logic  recognition
           -> ai_services.recognise_answer_images
                -> ai.services.run_task -> run_with_retries -> PROVIDER
      -> geminiAPI.grade_exam_logic                  grading, three phases
           phase 1  build_region_aware_evidence -> _build_diagram_prompt_parts
           phase 2  run_bounded(_grade_one_question_compute)
                     -> ai_services.grade_answer_with_parts -> PROVIDER
                     -> grading.result.parse_grading_response
           phase 3  _persist_and_report / _record_grading_failure
      -> examStats.add_exam_result_internal          aggregation
           -> grading.aggregation.aggregate_student_result
      -> exams.set_exam_stage                        workflow state

The stub is installed at the `TextTaskProvider` seam (`RecordingProvider`'s
place in conftest), so prompt assembly, part ordering, evidence composition,
retry, telemetry, validation, persistence, aggregation and the stage
transition are all the production code. Nothing here reimplements grading.

The question it answers: if the providers return valid output, does CogniGrade
carry it all the way to a correct final result -- and if one call fails, does
it fail safely without corrupting the siblings or pretending the exam is done?

NO LIVE PROVIDER -- zero API quota. Synthetic files in a temp directory, no
student data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select

from backend.ai.contracts import AITask, FilePart, TextPart
from backend.ai.errors import ProviderInvalidRequestError, ProviderTemporaryError
from backend.grading.aggregation import ExamResultStatus
from backend.models.files import AnswerScript
from backend.models.tables import (
    DocumentRegion,
    Exam,
    ExamResult,
    Question,
    QuestionResponse,
)
from backend.regions.schema import GeometryKind, RegionSource, RegionStatus, RegionType

#: `exams.EXAM_STAGE_GRADED` is the workflow's "this paper is finished" state.
from backend.routers.exams import EXAM_STAGE_GRADED, EXAM_STAGE_GRADING


# ---------------------------------------------------------------------------
# the synthetic exam
# ---------------------------------------------------------------------------
#
# Six questions, each present for a reason:
#
#   CG-Q1  5 marks   text-only answer, no regions        -> legacy path
#   CG-Q2  3 marks   marking-scheme AND student diagram  -> C1, fractional 2.5
#   CG-Q3  2 marks   accepted math + handwritten regions -> structured/mixed,
#                    plus a legacy table                    fractional 1.25
#   CG-Q4  2 marks   a genuinely wrong answer            -> a real 0.0
#   CG-Q5  4 marks   the failure-injection question
#   CG-Q6  3 marks   no response row at all              -> student submitted
#                                                           nothing
#
# The marker "CG-Qn" appears in each question's text, which is how the stub
# provider identifies which question a prompt is for -- from the real prompt,
# not from an out-of-band hint.

QUESTION_SPECS = [
    (1, "CG-Q1 explain the transport layer", 5.0),
    (2, "CG-Q2 read the attached diagram", 3.0),
    (3, "CG-Q3 derive the closed form", 2.0),
    (4, "CG-Q4 state the theorem", 2.0),
    (5, "CG-Q5 compare the two designs", 4.0),
    (6, "CG-Q6 optional extension", 3.0),
]

#: What recognition returns for each question, and what grading awards.
RECOGNISED_TEXT = {
    1: "SYNTHETIC-ANSWER-ONE segmentation and reassembly",
    2: "SYNTHETIC-ANSWER-TWO see my figure",
    3: "SYNTHETIC-ANSWER-THREE by induction",
    4: "SYNTHETIC-ANSWER-FOUR i do not know",
    5: "SYNTHETIC-ANSWER-FIVE both are pipelined",
}

SUCCESSFUL_GRADES = {1: 4.0, 2: 2.5, 3: 1.25, 4: 0.0, 5: 3.5}

#: Marking-scheme text, used to prove it never reaches a log.
MARKING_SCHEME_TEXT = "SYNTHETIC-SCHEME award one mark per named layer"


def _png(path: Path, colour) -> str:
    Image.new("RGB", (120, 120), colour).save(path, "PNG")
    return str(path)


class PipelineProvider:
    """A `TextTaskProvider` that answers both tasks deterministically.

    Installed under the name every task is configured with, so `run_task`
    resolves it exactly as it resolves the real adapter. It never touches a
    network and holds no per-invocation state beyond its call log.

    Recognition output is assembled in the wire format
    `process_answer_text_images_logic` actually parses ("Question Number N"
    sections), so the parsing, batching and mapping code is exercised rather
    than bypassed.
    """

    name = "gemini"

    def __init__(self, *, grades=None, image_to_question=None):
        #: question_number -> float score, or an exception instance to raise.
        self.grades = dict(grades or SUCCESSFUL_GRADES)
        #: answer-image path -> question_number, so a batch can be answered.
        self.image_to_question = dict(image_to_question or {})
        self.recognition_calls = []
        self.grading_calls = []

    # -- the provider interface --------------------------------------------

    async def run_text_task(self, request, settings, *, timeout_seconds=None):
        from backend.ai.contracts import ProviderResponse

        if request.task == AITask.ANSWER_RECOGNITION:
            body = self._recognise(request)
        elif request.task == AITask.GRADING:
            body = self._grade(request)
        else:  # pragma: no cover - the harness drives only these two
            raise AssertionError(f"unexpected task {request.task!r}")

        return ProviderResponse(
            text=body, provider=self.name, model=settings.model,
            task=request.task, prompt_version=request.prompt_version,
        )

    # -- per-task behaviour -------------------------------------------------

    def _recognise(self, request) -> str:
        numbers = [
            self.image_to_question[p]
            for p in request.file_paths
            if p in self.image_to_question
        ]
        self.recognition_calls.append(sorted(numbers))
        return "\n\n".join(
            f"Question Number {n}\n{RECOGNISED_TEXT[n]}" for n in sorted(numbers)
        )

    def _grade(self, request) -> str:
        number = self._question_number_of(request)
        self.grading_calls.append({
            "question_number": number,
            # Recorded at CALL time: the crops live in a workspace that is
            # deleted when grading finishes, so existence can only be checked
            # from inside the call.
            "files": [
                {"path": p.path, "exists": os.path.exists(p.path)}
                for p in request.parts if isinstance(p, FilePart)
            ],
            "texts": [p.text for p in request.parts if isinstance(p, TextPart)],
            "parts": list(request.parts),
        })

        outcome = self.grades.get(number)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:  # pragma: no cover - a fixture mistake, not a path
            raise AssertionError(f"no grade configured for CG-Q{number}")
        return json.dumps({"score": outcome, "reason": f"synthetic reason CG-Q{number}"})

    @staticmethod
    def _question_number_of(request) -> int:
        joined = "\n".join(p.text for p in request.parts if isinstance(p, TextPart))
        for number, text, _marks in QUESTION_SPECS:
            if f"CG-Q{number}" in joined:
                return number
        raise AssertionError("the grading prompt named no known question")

    # -- assertions helpers -------------------------------------------------

    def grading_call(self, number: int) -> dict:
        matching = [c for c in self.grading_calls if c["question_number"] == number]
        assert matching, f"CG-Q{number} was never sent to the grading provider"
        return matching[-1]

    @property
    def graded_numbers(self) -> list[int]:
        return [c["question_number"] for c in self.grading_calls]


@pytest_asyncio.fixture
async def pipeline(db, world, tmp_path, monkeypatch):
    """One synthetic exam, wired for the real task body.

    Returns a dict carrying the exam, its questions, the files and a `run`
    callable that executes `tasks._process_and_grade` against this session's
    engine.
    """
    import backend.tasks as tasks
    from backend.ai import providers

    owner, student = world["owner_prof"], world["student_a"]

    # Deterministic and sequential: this harness is about correctness, and the
    # bounded-concurrency helper has its own suite.
    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "1")
    monkeypatch.setenv("CG_AI__ANSWER_RECOGNITION__MAX_CONCURRENCY", "1")
    # One attempt per call unless a test asks for retries, so provider call
    # counts mean what they say.
    monkeypatch.setenv("CG_AI__GRADING__MAX_RETRIES", "0")
    monkeypatch.setenv("CG_AI__ANSWER_RECOGNITION__MAX_RETRIES", "0")

    exam = Exam(
        title="Offline Pipeline Exam", classroom_id=world["class_a"].id,
        author_id=owner.id, exam_stage=5,
    )
    db.add(exam)
    await db.commit()
    await db.refresh(exam)

    questions = {}
    for number, text, marks in QUESTION_SPECS:
        q = Question(
            exam_id=exam.id, question_number=number, text=text, max_marks=marks,
            ideal_marking_scheme=MARKING_SCHEME_TEXT,
        )
        db.add(q)
        questions[number] = q
    await db.commit()
    for q in questions.values():
        await db.refresh(q)

    # --- synthetic files -----------------------------------------------
    files = {"answer_images": {}}
    for number in RECOGNISED_TEXT:
        files["answer_images"][number] = _png(
            tmp_path / f"ans-q{number}.png", (10 * number, 40, 200)
        )
    files["ms_diagram"] = _png(tmp_path / "ms-diagram.png", (0, 128, 0))
    files["student_diagram"] = _png(tmp_path / "student-diagram.png", (128, 0, 0))
    files["legacy_table"] = _png(tmp_path / "legacy-table.png", (0, 0, 128))
    # The answer script the structured regions are cut from. A raster, so the
    # renderer needs no PDF backend for this fixture.
    files["script"] = _png(tmp_path / "answer-script.png", (240, 240, 240))

    script = AnswerScript(
        title="offline.png", file_path=files["script"],
        exam_id=exam.id, student_id=student.id,
    )
    db.add(script)

    # CG-Q2 carries reference AND student visual evidence: the C1 pair.
    questions[2].ms_diagram_images = json.dumps([files["ms_diagram"]])

    responses = {}
    for number in RECOGNISED_TEXT:
        qr = QuestionResponse(
            question_id=questions[number].id, student_id=student.id,
            ans_text_images=json.dumps([files["answer_images"][number]]),
        )
        responses[number] = qr
        db.add(qr)
    # CG-Q6 deliberately has NO response row: the student submitted nothing.
    responses[2].ans_diagram_images = json.dumps([files["student_diagram"]])
    responses[3].ans_table_images = json.dumps([files["legacy_table"]])
    await db.commit()
    for qr in responses.values():
        await db.refresh(qr)
    await db.refresh(script)

    # --- CG-Q3's structured regions -------------------------------------
    # An accepted MATH region (attached, in its own category) and an accepted
    # HANDWRITTEN_TEXT region (represented, never attached -- the recognised
    # answer_text already carries it). The legacy table above must survive
    # both, because neither covers the table category.
    db.add_all([
        DocumentRegion(
            exam_id=exam.id, answer_script_id=script.id, page_index=0,
            region_type=RegionType.MATH, geometry_kind=GeometryKind.RECT,
            geometry=json.dumps({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.3}),
            question_id=questions[3].id, reading_order=0,
            status=RegionStatus.ACCEPTED, source=RegionSource.MODEL,
        ),
        DocumentRegion(
            exam_id=exam.id, answer_script_id=script.id, page_index=0,
            region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
            geometry=json.dumps({"x": 0.1, "y": 0.5, "w": 0.6, "h": 0.2}),
            question_id=questions[3].id, reading_order=1,
            status=RegionStatus.ACCEPTED, source=RegionSource.MODEL,
        ),
    ])
    await db.commit()

    image_to_question = {p: n for n, p in files["answer_images"].items()}

    state = {
        "exam": exam, "questions": questions, "responses": responses,
        "student": student, "script": script, "files": files,
        "image_to_question": image_to_question,
        "provider": None,
    }

    async def run(*, grades=None):
        """Execute the real Celery task body once, and return the provider."""
        provider = PipelineProvider(
            grades=grades, image_to_question=image_to_question
        )
        providers.register_provider(provider.name, provider)
        monkeypatch.setattr(tasks, "AsyncSessionLocal", session_factory_of(db))
        # The harness session must not hold rows the task is about to rewrite.
        await db.commit()
        db.expunge_all()
        await tasks._process_and_grade(exam.id, student.id)
        db.expunge_all()
        state["provider"] = provider
        return provider

    state["run"] = run
    yield state
    providers.reset_providers()


def session_factory_of(db):
    """A zero-argument factory yielding a session on the harness engine.

    `_process_and_grade` opens its own session, exactly as the worker does;
    this only points it at the test database instead of production.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# reading the outcome back
# ---------------------------------------------------------------------------

async def _responses_by_number(db, pipeline) -> dict:
    rows = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.student_id == pipeline["student"].id,
        QuestionResponse.question_id.in_([q.id for q in pipeline["questions"].values()]),
    ))).scalars().all()
    by_question_id = {r.question_id: r for r in rows}
    return {
        number: by_question_id.get(q.id)
        for number, q in pipeline["questions"].items()
    }


async def _exam_result(db, pipeline):
    return (await db.execute(select(ExamResult).where(
        ExamResult.exam_id == pipeline["exam"].id,
        ExamResult.student_id == pipeline["student"].id,
    ))).scalars().first()


async def _exam_stage(db, pipeline) -> int:
    exam = (await db.execute(select(Exam).where(
        Exam.id == pipeline["exam"].id
    ))).scalars().first()
    return exam.exam_stage


# ---------------------------------------------------------------------------
# 1. the whole exam succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_full_exam_success(db, pipeline):
    """Every question with a response is graded, and the exam finalises."""
    provider = await pipeline["run"]()

    rows = await _responses_by_number(db, pipeline)
    for number, expected in SUCCESSFUL_GRADES.items():
        row = rows[number]
        assert row is not None, f"CG-Q{number} lost its response row"
        assert float(row.marks_obtained) == expected, f"CG-Q{number}"
        assert row.reasoning == f"synthetic reason CG-Q{number}"
        assert row.grading_error_code is None, "a success carried a failure code"
        assert row.answer_text == RECOGNISED_TEXT[number], "recognition did not land"

    assert rows[6] is None, "a response row was invented for an unanswered question"

    result = await _exam_result(db, pipeline)
    assert result is not None
    assert float(result.marks_obtained) == pytest.approx(11.25)
    assert result.status == ExamResultStatus.GRADED
    assert result.graded_at is not None, "a final result must carry a timestamp"

    assert await _exam_stage(db, pipeline) == EXAM_STAGE_GRADED

    assert sorted(provider.graded_numbers) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_an_unanswered_question_does_not_block_finalisation(db, pipeline):
    """CG-Q6 has no response row: nothing was submitted, so nothing failed."""
    provider = await pipeline["run"]()

    assert 6 not in provider.graded_numbers, "a provider call was made for no evidence"
    result = await _exam_result(db, pipeline)
    assert result.status == ExamResultStatus.GRADED
    assert result.graded_at is not None


# ---------------------------------------------------------------------------
# 2. one question fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_partial_grading_failure(db, pipeline):
    """CG-Q5's provider call fails. Nothing else may move."""
    grades = dict(SUCCESSFUL_GRADES)
    grades[5] = ProviderInvalidRequestError("synthetic failure", provider="gemini")
    await pipeline["run"](grades=grades)

    rows = await _responses_by_number(db, pipeline)

    failed = rows[5]
    assert failed.marks_obtained is None, "a failed question was scored"
    assert failed.grading_error_code == "invalid_request"

    for number in (1, 2, 3, 4):
        row = rows[number]
        assert float(row.marks_obtained) == SUCCESSFUL_GRADES[number], (
            f"CG-Q{number} was rolled back by a sibling's failure"
        )
        assert row.grading_error_code is None

    result = await _exam_result(db, pipeline)
    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert result.graded_at is None, "an incomplete run was stamped as final"
    # The running total is still recorded so partial progress is visible.
    assert float(result.marks_obtained) == pytest.approx(7.75)

    assert await _exam_stage(db, pipeline) == EXAM_STAGE_GRADING, (
        "the exam was advanced to the finished stage despite an incomplete result"
    )


@pytest.mark.asyncio
async def test_offline_real_zero_vs_failure(db, pipeline):
    """The distinction the whole of C6 exists to protect."""
    grades = dict(SUCCESSFUL_GRADES)
    grades[5] = ProviderInvalidRequestError("synthetic failure", provider="gemini")
    await pipeline["run"](grades=grades)

    rows = await _responses_by_number(db, pipeline)

    # CG-Q4 answered badly: a real, earned zero.
    assert rows[4].marks_obtained is not None
    assert float(rows[4].marks_obtained) == 0.0
    assert rows[4].grading_error_code is None
    assert rows[4].reasoning == "synthetic reason CG-Q4"

    # CG-Q5 was never graded: absent, not zero.
    assert rows[5].marks_obtained is None
    assert rows[5].grading_error_code == "invalid_request"
    assert rows[5].reasoning is None

    # And the difference survives aggregation.
    result = await _exam_result(db, pipeline)
    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert float(result.marks_obtained) == pytest.approx(7.75), (
        "the zero was dropped, or the failure was counted as zero"
    )


# ---------------------------------------------------------------------------
# 3. fractional marks, end to end (C7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_fractional_marks_round_trip(db, pipeline):
    """2.5 and 1.25 survive provider -> persistence -> aggregation."""
    await pipeline["run"]()

    rows = await _responses_by_number(db, pipeline)
    assert float(rows[2].marks_obtained) == pytest.approx(2.5)
    assert float(rows[3].marks_obtained) == pytest.approx(1.25)

    result = await _exam_result(db, pipeline)
    assert float(result.marks_obtained) == pytest.approx(11.25)
    # Not silently truncated to an integer at any layer.
    assert float(result.marks_obtained) != 11.0


# ---------------------------------------------------------------------------
# 4. C1 through the real call path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_reference_and_student_evidence_stay_separate(db, pipeline):
    """AUDIT C1, asserted on the parts the provider actually received."""
    from backend.ai.prompts.grading import (
        REFERENCE_IMAGE_HEADING,
        STUDENT_IMAGE_HEADING,
    )

    provider = await pipeline["run"]()
    call = provider.grading_call(2)

    parts = call["parts"]
    texts = [p.text if isinstance(p, TextPart) else None for p in parts]
    ref_marker = texts.index(REFERENCE_IMAGE_HEADING)
    student_marker = texts.index(STUDENT_IMAGE_HEADING)
    assert ref_marker < student_marker, "reference material must precede student evidence"

    ref_file = parts[ref_marker + 1]
    student_file = parts[student_marker + 1]
    ms_path = pipeline["files"]["ms_diagram"]
    student_path = pipeline["files"]["student_diagram"]

    assert ref_file.path == ms_path, "the reference slot did not hold the marking scheme"
    assert student_file.path == student_path, "the student slot did not hold the answer"
    assert ref_file.path != student_path, (
        "REGRESSION: the student's image reached the marking-scheme slot"
    )
    sent = [f["path"] for f in call["files"]]
    assert sent.count(ms_path) == 1 and sent.count(student_path) == 1


# ---------------------------------------------------------------------------
# 5. structured regions and legacy crops, composed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_structured_and_legacy_evidence(db, pipeline):
    """CG-Q1 legacy-only, CG-Q3 structured maths beside a legacy table."""
    provider = await pipeline["run"]()

    # CG-Q1 has no regions and no image columns: text only, nothing attached.
    assert provider.grading_call(1)["files"] == []

    call = provider.grading_call(3)
    paths = [f["path"] for f in call["files"]]
    legacy_table = pipeline["files"]["legacy_table"]

    assert legacy_table in paths, (
        "the legacy table was erased by an unrelated structured category"
    )
    structured = [p for p in paths if p != legacy_table]
    assert len(structured) == 1, "expected exactly the maths crop beside the table"
    assert all(f["exists"] for f in call["files"]), (
        "a crop was deleted before the provider was called"
    )

    # The handwritten-text region attaches nothing: the recognised answer_text
    # already carries it, and sending both would duplicate the answer.
    prompt = "\n".join(call["texts"])
    assert RECOGNISED_TEXT[3] in prompt
    assert len(paths) == 2, "handwritten text was attached alongside its transcription"

    # Maths is described as maths. It used to be filed under `diagram`.
    assert "mathematical working" in prompt
    assert "diagram" not in prompt


# ---------------------------------------------------------------------------
# 6. fail-closed when the source document is gone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_source_missing_fails_closed(db, pipeline):
    """Structured evidence was expected and could not be produced.

    Grading a stale legacy crop instead would produce a plausible wrong mark,
    so the question fails with no mark -- and the siblings carry on.
    """
    script = (await db.execute(select(AnswerScript).where(
        AnswerScript.id == pipeline["script"].id
    ))).scalars().first()
    script.file_path = str(Path(pipeline["files"]["script"]).parent / "gone.png")
    await db.commit()

    provider = await pipeline["run"]()

    rows = await _responses_by_number(db, pipeline)
    assert rows[3].marks_obtained is None, "stale evidence produced a mark"
    assert rows[3].grading_error_code == "source_missing"
    assert 3 not in provider.graded_numbers, "a provider call was made without evidence"

    for number in (1, 2, 4, 5):
        assert float(rows[number].marks_obtained) == SUCCESSFUL_GRADES[number]

    result = await _exam_result(db, pipeline)
    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert result.graded_at is None
    assert await _exam_stage(db, pipeline) == EXAM_STAGE_GRADING


# ---------------------------------------------------------------------------
# 7. provider call counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_provider_call_counts(db, pipeline):
    """One recognition batch, one grading call per question with evidence."""
    provider = await pipeline["run"]()

    # Five answer images, batched five at a time: exactly one recognition call.
    assert len(provider.recognition_calls) == 1
    assert provider.recognition_calls[0] == [1, 2, 3, 4, 5]

    assert len(provider.grading_calls) == 5, "a question was graded twice"
    assert sorted(provider.graded_numbers) == [1, 2, 3, 4, 5]
    assert 6 not in provider.graded_numbers


@pytest.mark.asyncio
async def test_a_retryable_failure_is_retried_exactly_to_the_budget(
    db, pipeline, monkeypatch
):
    """Extra attempts happen only where retry policy says they should."""
    monkeypatch.setenv("CG_AI__GRADING__MAX_RETRIES", "2")
    monkeypatch.setenv("CG_AI__GRADING__RETRY_BASE_DELAY", "0")

    grades = dict(SUCCESSFUL_GRADES)
    grades[5] = ProviderTemporaryError("synthetic outage", provider="gemini")
    provider = await pipeline["run"](grades=grades)

    attempts = [n for n in provider.graded_numbers if n == 5]
    assert len(attempts) == 3, "retry budget was not honoured (1 try + 2 retries)"
    # The others are still called exactly once each.
    for number in (1, 2, 3, 4):
        assert provider.graded_numbers.count(number) == 1

    rows = await _responses_by_number(db, pipeline)
    assert rows[5].marks_obtained is None
    assert rows[5].grading_error_code == "temporary"


# ---------------------------------------------------------------------------
# 8. running it again
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_reexecution_after_success_is_idempotent(db, pipeline):
    """A second complete run must not duplicate rows or double the total."""
    await pipeline["run"]()
    first = await _exam_result(db, pipeline)
    first_id, first_total = first.id, float(first.marks_obtained)

    await pipeline["run"]()

    results = (await db.execute(select(ExamResult).where(
        ExamResult.exam_id == pipeline["exam"].id,
        ExamResult.student_id == pipeline["student"].id,
    ))).scalars().all()
    assert len(results) == 1, "a second run created a duplicate ExamResult"
    assert results[0].id == first_id
    assert float(results[0].marks_obtained) == first_total, "the total was doubled"

    responses = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.student_id == pipeline["student"].id,
        QuestionResponse.question_id.in_(
            [q.id for q in pipeline["questions"].values()]
        ),
    ))).scalars().all()
    assert len(responses) == 5, "a second run created duplicate response rows"


@pytest.mark.asyncio
async def test_offline_rerunning_after_a_failure_heals_the_exam(db, pipeline):
    """The recovery path: fix the provider, run again, and the exam finalises."""
    grades = dict(SUCCESSFUL_GRADES)
    grades[5] = ProviderInvalidRequestError("synthetic failure", provider="gemini")
    await pipeline["run"](grades=grades)

    incomplete = await _exam_result(db, pipeline)
    assert incomplete.status == ExamResultStatus.GRADING_INCOMPLETE
    assert await _exam_stage(db, pipeline) == EXAM_STAGE_GRADING

    await pipeline["run"]()

    rows = await _responses_by_number(db, pipeline)
    assert float(rows[5].marks_obtained) == SUCCESSFUL_GRADES[5]
    assert rows[5].grading_error_code is None, (
        "a stale failure code outlived the failure it described"
    )

    healed = await _exam_result(db, pipeline)
    assert healed.id == incomplete.id
    assert healed.status == ExamResultStatus.GRADED
    assert healed.graded_at is not None
    assert float(healed.marks_obtained) == pytest.approx(11.25)
    assert await _exam_stage(db, pipeline) == EXAM_STAGE_GRADED


# ---------------------------------------------------------------------------
# 9. logging must not leak the paper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_run_logs_no_student_or_scheme_content(db, pipeline, caplog):
    """CogniGrade's own logging must carry ids and categories, never content.

    Scoped to the `backend` logger tree, which is what a deployment ships to a
    log aggregator. The SQLite driver's DEBUG statement log is deliberately not
    included: every DB driver echoes bound parameters at DEBUG, that is a
    logging-level decision rather than something this code controls, and
    asserting on it would be asserting about aiosqlite.
    """
    import logging

    grades = dict(SUCCESSFUL_GRADES)
    grades[5] = ProviderInvalidRequestError("synthetic failure", provider="gemini")

    caplog.set_level(logging.INFO, logger="backend")
    await pipeline["run"](grades=grades)

    ours = [r for r in caplog.records if r.name.startswith("backend")]
    assert ours, "the run produced no application log at all"
    text = "\n".join(r.getMessage() for r in ours)

    for answer in RECOGNISED_TEXT.values():
        assert answer not in text, "a student's recognised answer was logged"
    assert MARKING_SCHEME_TEXT not in text, "the marking scheme was logged"
    assert "synthetic reason CG-Q1" not in text, "provider output was logged"
    for path in pipeline["files"]["answer_images"].values():
        assert path not in text, "a private evidence path was logged"

    # The safe half is still there: ids and a provider-neutral category.
    assert "invalid_request" in text
    assert f"exam_id={pipeline['exam'].id}" in text
