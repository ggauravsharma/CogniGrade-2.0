"""Bounded concurrency for exam grading.

The risk this phase creates is not slowness, it is corruption: an
`AsyncSession` used by two coroutines at once, a failure cancelling its
siblings, a later write clobbering an earlier one, or completion order leaking
into what a professor reads. These tests are about those, not about speed --
except for one benchmark that proves the mechanism does something.

No provider is contacted. Everything runs against a recording/instrumented
fake, so the suite costs nothing and cannot flake on a network.
"""

import asyncio
import time

import pytest
from sqlalchemy import select

from backend.ai.concurrency import Outcome, run_bounded
from backend.ai.config import get_task_settings
from backend.ai.contracts import AITask, ProviderResponse
from backend.ai.errors import ProviderRateLimitError, ProviderTemporaryError
from backend.grading.aggregation import ExamResultStatus
from backend.models.tables import ExamResult, Question, QuestionResponse
from backend.routers.examStats import add_exam_result_internal
from backend.routers.geminiAPI import grade_exam_logic


# ---------------------------------------------------------------------------
# the primitive
# ---------------------------------------------------------------------------

class ConcurrencyProbe:
    """Records the maximum number of workers in flight at once."""

    def __init__(self):
        self.current = 0
        self.peak = 0
        self.order = []

    async def work(self, item, delay=0.01, fail=None):
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(delay)
            if fail is not None:
                raise fail
            self.order.append(item)
            return item
        finally:
            self.current -= 1


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_the_configured_limit_is_never_exceeded(limit):
    probe = ConcurrencyProbe()
    items = list(range(12))

    outcomes = await run_bounded(items, probe.work, limit=limit)

    assert probe.peak <= limit, f"{probe.peak} in flight with a limit of {limit}"
    assert len(outcomes) == len(items)
    assert all(o.ok for o in outcomes)


@pytest.mark.asyncio
async def test_a_limit_of_one_is_strictly_sequential():
    """`max_concurrency = 1` must restore the previous behaviour exactly."""
    probe = ConcurrencyProbe()
    await run_bounded(list(range(6)), probe.work, limit=1)
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_work_really_overlaps_when_the_limit_allows_it():
    """Not "gather was called" -- actual observed overlap."""
    probe = ConcurrencyProbe()
    await run_bounded(list(range(8)), lambda i: probe.work(i, delay=0.05), limit=4)
    assert probe.peak >= 2, "no two workers ever ran at the same time"


@pytest.mark.asyncio
async def test_results_come_back_in_input_order_not_completion_order():
    """Provider finishing Q4, Q1, Q3, Q2 must still read Q1..Q4."""
    delays = {0: 0.04, 1: 0.01, 2: 0.03, 3: 0.005}

    async def worker(item):
        await asyncio.sleep(delays[item])
        return item

    outcomes = await run_bounded([0, 1, 2, 3], worker, limit=4)
    assert [o.item for o in outcomes] == [0, 1, 2, 3]
    assert [o.value for o in outcomes] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_one_failure_does_not_cancel_the_others():
    """`asyncio.gather`'s default would abandon the rest of the paper."""

    async def worker(item):
        await asyncio.sleep(0.01)
        if item == 2:
            raise ProviderRateLimitError("429")
        return item * 10

    outcomes = await run_bounded([0, 1, 2, 3, 4], worker, limit=2)

    assert [o.ok for o in outcomes] == [True, True, False, True, True]
    assert outcomes[2].error.category == "rate_limit"
    assert [o.value for o in outcomes if o.ok] == [0, 10, 30, 40]


@pytest.mark.asyncio
async def test_several_independent_failures_are_recorded_separately():
    async def worker(item):
        if item % 2 == 0:
            raise ProviderTemporaryError(f"failed {item}")
        return item

    outcomes = await run_bounded(list(range(6)), worker, limit=3)
    failed = [o for o in outcomes if not o.ok]
    assert [o.item for o in failed] == [0, 2, 4]
    assert [str(o.error) for o in failed] == ["failed 0", "failed 2", "failed 4"]


@pytest.mark.asyncio
async def test_an_empty_batch_is_not_an_error():
    assert await run_bounded([], lambda i: None, limit=3) == []


@pytest.mark.asyncio
async def test_a_zero_or_negative_limit_falls_back_to_sequential():
    probe = ConcurrencyProbe()
    await run_bounded(list(range(4)), probe.work, limit=0)
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_cancelling_the_caller_does_not_leave_tasks_running():
    running = {"count": 0}

    async def worker(item):
        running["count"] += 1
        try:
            await asyncio.sleep(5)
            return item
        finally:
            running["count"] -= 1

    task = asyncio.create_task(run_bounded(list(range(6)), worker, limit=3))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert running["count"] == 0, "workers survived the cancellation"


@pytest.mark.asyncio
async def test_the_semaphore_binds_to_the_running_loop():
    """Celery drives this through run_until_complete, not a server loop.

    A module-level Semaphore would bind to whichever loop imported it. Running
    the same helper twice on two different loops proves it does not.
    """
    probe = ConcurrencyProbe()

    def run_once():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                run_bounded(list(range(4)), probe.work, limit=2)
            )
        finally:
            loop.close()

    first = await asyncio.to_thread(run_once)
    second = await asyncio.to_thread(run_once)
    assert len(first) == len(second) == 4
    assert all(o.ok for o in first + second)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_grading_has_a_conservative_default_concurrency():
    settings = get_task_settings(AITask.GRADING)
    assert 2 <= settings.max_concurrency <= 4, settings.max_concurrency


def test_single_call_tasks_stay_sequential():
    """One document, one call: a limit above 1 would be meaningless."""
    for task in (AITask.DOCUMENT_EXTRACTION, AITask.LABEL_EXTRACTION):
        assert get_task_settings(task).max_concurrency == 1


def test_recognition_is_bounded_rather_than_unbounded():
    """Recognition batches were ALREADY concurrent, via a bare gather.

    This phase capped them; it did not introduce the concurrency. A limit of 1
    would be a slowdown of existing behaviour, so the cap is conservative
    rather than sequential.
    """
    for task in (AITask.ANSWER_RECOGNITION, AITask.MARKING_SCHEME_RECOGNITION):
        limit = get_task_settings(task).max_concurrency
        assert 2 <= limit <= 4, limit


def test_no_unbounded_gather_remains_in_the_grading_router():
    """A bare `asyncio.gather` over provider calls is the thing being removed.

    Parsed rather than grepped, so the docstring that EXPLAINS why gather is
    wrong does not itself trip the check.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("backend/routers/geminiAPI.py").read_text(encoding="utf-8")
    )
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func
            if target.attr == "gather" and getattr(target.value, "id", None) == "asyncio":
                calls.append(node.lineno)
    assert calls == [], f"unbounded gather at line(s) {calls}; use run_bounded"


def test_concurrency_is_configurable_per_task(monkeypatch):
    baseline = get_task_settings(AITask.ANSWER_RECOGNITION).max_concurrency
    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "8")
    assert get_task_settings(AITask.GRADING).max_concurrency == 8
    assert get_task_settings(AITask.ANSWER_RECOGNITION).max_concurrency == baseline


def test_concurrency_can_be_pinned_to_one_as_an_emergency_control(monkeypatch):
    """The quota escape hatch: restore sequential grading without a deploy."""
    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "1")
    assert get_task_settings(AITask.GRADING).max_concurrency == 1


@pytest.mark.parametrize("raw", ["0", "-4"])
def test_a_nonsense_limit_is_clamped_not_obeyed(monkeypatch, raw):
    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", raw)
    assert get_task_settings(AITask.GRADING).max_concurrency == 1


# ---------------------------------------------------------------------------
# retry interaction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_retry_inside_a_worker_still_respects_the_bound():
    """A retrying question holds its slot; it must not open a second one."""
    probe = ConcurrencyProbe()
    attempts = {}

    async def worker(item):
        probe.current += 1
        probe.peak = max(probe.peak, probe.current)
        try:
            attempts[item] = attempts.get(item, 0) + 1
            await asyncio.sleep(0.01)
            if item == 1 and attempts[item] == 1:
                # the service layer would retry internally; simulate that here
                await asyncio.sleep(0.02)
            return item
        finally:
            probe.current -= 1

    await run_bounded([0, 1, 2, 3, 4, 5], worker, limit=2)
    assert probe.peak <= 2


@pytest.mark.asyncio
async def test_the_orchestrator_adds_no_retry_of_its_own(fake_provider, db, world):
    """3 attempts total, not 9: the exam layer must trust the service contract."""
    provider = fake_provider(errors=[ProviderTemporaryError("503")] * 10)
    exam = await _fresh_exam(db, world)
    student = world["student_a"]

    await _add_question(db, exam.id, 1, max_marks=5)
    await _seed_response(db, exam, student, 1)

    settings = get_task_settings(AITask.GRADING)
    await grade_exam_logic(exam.id, student.id, db)

    expected = settings.max_retries + 1
    assert len(provider.requests) == expected, (
        f"{len(provider.requests)} provider calls for one question; "
        f"the service retries {expected} times and the exam layer must add none"
    )


# ---------------------------------------------------------------------------
# end to end through grade_exam_logic
# ---------------------------------------------------------------------------

async def _fresh_exam(db, world):
    """An exam of this suite's own.

    The shared `world` fixture already seeds one question and two responses in
    `exam_a`; grading those too would make every count in these tests depend on
    a fixture that exists for the authorization suite.
    """
    from backend.models.tables import Exam

    exam = Exam(
        title="Concurrency Exam", classroom_id=world["class_a"].id,
        author_id=world["owner_prof"].id, exam_stage=6,
    )
    db.add(exam)
    await db.commit()
    await db.refresh(exam)
    return exam


async def _add_question(db, exam_id, number, max_marks=10):
    q = Question(
        exam_id=exam_id, question_number=number, text=f"Q{number}",
        max_marks=max_marks, ideal_marking_scheme=f"scheme {number}",
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _seed_response(db, exam, student, question_number, answer="an answer"):
    found = await db.execute(select(Question).where(
        Question.exam_id == exam.id, Question.question_number == question_number
    ))
    question = found.scalars().first()
    qr = QuestionResponse(
        question_id=question.id, student_id=student.id,
        answer_text=answer, marks_obtained=None,
    )
    db.add(qr)
    await db.commit()
    await db.refresh(qr)
    return qr


async def _build_paper(db, exam, student, count, *, max_marks=10):
    for number in range(1, count + 1):
        await _add_question(db, exam.id, number, max_marks=max_marks)
        await _seed_response(db, exam, student, number)


@pytest.mark.asyncio
async def test_a_whole_paper_is_graded_and_persisted(fake_provider, db, world):
    provider = fake_provider(body='{"score": 4, "reason": "good"}')
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 5)

    summary = await grade_exam_logic(exam.id, student.id, db)

    assert summary["failed_count"] == 0
    assert summary["graded_count"] >= 5
    assert len(provider.requests) >= 5

    db.expunge_all()
    rows = (await db.execute(
        select(QuestionResponse).join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam.id, QuestionResponse.student_id == student.id)
    )).scalars().all()
    graded = [r for r in rows if r.marks_obtained is not None]
    assert len(graded) >= 5
    assert all(r.grading_error_code is None for r in graded)


@pytest.mark.asyncio
async def test_one_failing_question_leaves_the_others_graded(fake_provider, db, world):
    """Q2 fails; Q1, Q3, Q4 must still be graded and persisted."""
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 4)

    def body(request):
        # The failing one is identified from the prompt, since completion order
        # is not deterministic.
        if "Q2" in request.prompt_text:
            raise ProviderRateLimitError("429")
        return '{"score": 3, "reason": "ok"}'

    fake_provider(body=body)
    summary = await grade_exam_logic(exam.id, student.id, db)

    failed_numbers = [f["question_number"] for f in summary["failed_questions"]]
    assert 2 in failed_numbers
    assert summary["graded_count"] >= 3

    db.expunge_all()
    rows = {}
    for row in (await db.execute(
        select(QuestionResponse, Question.question_number)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam.id, QuestionResponse.student_id == student.id)
    )).all():
        rows[row[1]] = row[0]

    assert rows[2].marks_obtained is None, "a failure must never be scored"
    assert rows[2].grading_error_code == "rate_limit"
    for number in (1, 3, 4):
        assert rows[number].marks_obtained == 3
        assert rows[number].grading_error_code is None


@pytest.mark.asyncio
async def test_failed_questions_are_reported_in_question_order(fake_provider, db, world):
    """Completion order is nondeterministic; the report must not be."""
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 6)

    async def body(request):  # noqa: D401 - callable body
        return None

    def choose(request):
        text = request.prompt_text
        for number in (5, 2):
            if f"Q{number}" in text:
                raise ProviderTemporaryError("503")
        return '{"score": 1, "reason": "ok"}'

    fake_provider(body=choose)
    summary = await grade_exam_logic(exam.id, student.id, db)

    numbers = [f["question_number"] for f in summary["failed_questions"]]
    assert numbers == sorted(numbers), numbers
    assert numbers == [2, 5]
    assert [r["question_number"] for r in summary["results"]] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_a_failure_leaves_the_result_incomplete_not_zero(fake_provider, db, world):
    """C6 under concurrency."""
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 3)

    def choose(request):
        if "Q3" in request.prompt_text:
            raise ProviderTemporaryError("503")
        return '{"score": 2, "reason": "ok"}'

    fake_provider(body=choose)
    await grade_exam_logic(exam.id, student.id, db)
    await add_exam_result_internal(exam.id, student.id, db)

    db.expunge_all()
    result = (await db.execute(select(ExamResult).where(
        ExamResult.exam_id == exam.id, ExamResult.student_id == student.id
    ))).scalars().first()

    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert result.graded_at is None


@pytest.mark.asyncio
async def test_fractional_marks_survive_concurrent_grading(fake_provider, db, world):
    """C7 under concurrency: each question keeps its own exact value."""
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 3, max_marks=5)

    scores = {1: 1.5, 2: 2.25, 3: 0.5}

    def choose(request):
        text = request.prompt_text
        for number, score in scores.items():
            if f"Q{number}" in text:
                return f'{{"score": {score}, "reason": "ok"}}'
        return '{"score": 0, "reason": "ok"}'

    fake_provider(body=choose)
    await grade_exam_logic(exam.id, student.id, db)

    db.expunge_all()
    got = {}
    for row in (await db.execute(
        select(QuestionResponse, Question.question_number)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam.id, QuestionResponse.student_id == student.id)
    )).all():
        got[row[1]] = row[0].marks_obtained

    for number, score in scores.items():
        assert got[number] == score, f"Q{number} came back as {got[number]}"


@pytest.mark.asyncio
async def test_each_question_is_written_exactly_once(fake_provider, db, world):
    """No race in which a later outcome overwrites an earlier one."""
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 5)

    counter = {"n": 0}

    def body(request):
        counter["n"] += 1
        return '{"score": 2, "reason": "ok"}'

    provider = fake_provider(body=body)
    await grade_exam_logic(exam.id, student.id, db)

    db.expunge_all()
    rows = (await db.execute(
        select(QuestionResponse).join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam.id, QuestionResponse.student_id == student.id)
    )).scalars().all()

    # One provider call per question, one row per question, all with the mark.
    assert len(provider.requests) == len(rows)
    assert all(r.marks_obtained == 2 for r in rows)


@pytest.mark.asyncio
async def test_sequential_and_concurrent_produce_the_same_records(
    fake_provider, db, world, monkeypatch
):
    """concurrency=1 and concurrency=4 must be indistinguishable in the data."""
    exam = await _fresh_exam(db, world)
    student_a, student_b = world["student_a"], world["student_b"]
    await _build_paper(db, exam, student_a, 4)
    for number in range(1, 5):
        await _seed_response(db, exam, student_b, number)

    fake_provider(body='{"score": 3, "reason": "ok"}')

    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "1")
    serial = await grade_exam_logic(exam.id, student_a.id, db)

    monkeypatch.setenv("CG_AI__GRADING__MAX_CONCURRENCY", "4")
    parallel = await grade_exam_logic(exam.id, student_b.id, db)

    assert serial["concurrency"] == 1
    assert parallel["concurrency"] == 4
    strip = lambda s: [(r["question_number"], r["status"], r["grade"]) for r in s["results"]]
    assert strip(serial) == strip(parallel)


@pytest.mark.asyncio
async def test_the_run_reports_orchestration_metadata(fake_provider, db, world, caplog):
    fake_provider(body='{"score": 1, "reason": "ok"}')
    exam = await _fresh_exam(db, world)
    student = world["student_a"]
    await _build_paper(db, exam, student, 3)

    with caplog.at_level("INFO", logger="backend.routers.geminiAPI"):
        summary = await grade_exam_logic(exam.id, student.id, db)

    assert "exam_grading_run" in caplog.text
    assert f"exam_id={exam.id}" in caplog.text
    assert "concurrency=" in caplog.text
    assert "duration_ms=" in caplog.text
    assert summary["duration_ms"] >= 0
    assert "an answer" not in caplog.text, "answer text must not be logged"


# ---------------------------------------------------------------------------
# the Celery bridge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aggregation_runs_only_after_every_question_is_graded(monkeypatch):
    """Part K: the final result must not be computed mid-flight.

    Exercises the real `_process_and_grade` body with each step replaced by a
    recorder, so the ORDER is asserted rather than assumed.
    """
    import backend.tasks as tasks

    calls = []

    async def _recognise(exam_id, student_id, db):
        calls.append("recognise")

    async def _grade(exam_id, student_id, db):
        calls.append("grade_start")
        await asyncio.sleep(0.01)
        calls.append("grade_end")

    async def _aggregate(exam_id, student_id, db):
        calls.append("aggregate")

    async def _is_final(exam_id, student_id, db):
        calls.append("check_final")
        return True

    stages = []

    async def _stage(exam_id, stage, db):
        calls.append("stage")
        stages.append(stage)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tasks, "process_answer_text_images_logic", _recognise)
    monkeypatch.setattr(tasks, "grade_exam_logic", _grade)
    monkeypatch.setattr(tasks, "add_exam_result_internal", _aggregate)
    monkeypatch.setattr(tasks, "exam_result_is_final", _is_final)
    monkeypatch.setattr(tasks, "set_exam_stage", _stage)
    monkeypatch.setattr(tasks, "AsyncSessionLocal", lambda: _Session())

    await tasks._process_and_grade(1, 2)

    assert calls == [
        "recognise", "grade_start", "grade_end", "aggregate", "check_final", "stage",
    ]
    assert calls.index("grade_end") < calls.index("aggregate"), (
        "the exam result was computed while grading was still running"
    )
    assert calls.index("aggregate") < calls.index("check_final"), (
        "finality was read before aggregation had written it"
    )
    assert stages == [tasks.EXAM_STAGE_GRADED]


@pytest.mark.asyncio
async def test_an_incomplete_result_does_not_reach_the_graded_stage(monkeypatch):
    """The stage must come from the aggregation's verdict, not from arriving.

    The task used to end in an unconditional `set_exam_stage(exam_id, 7, db)`,
    so a run that had just written `grading_incomplete` still marked the exam
    Graded -- two records of the same fact disagreeing.
    """
    import backend.tasks as tasks

    stages = []

    async def _noop(*a, **kw):
        return None

    async def _not_final(exam_id, student_id, db):
        return False

    async def _stage(exam_id, stage, db):
        stages.append(stage)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tasks, "process_answer_text_images_logic", _noop)
    monkeypatch.setattr(tasks, "grade_exam_logic", _noop)
    monkeypatch.setattr(tasks, "add_exam_result_internal", _noop)
    monkeypatch.setattr(tasks, "exam_result_is_final", _not_final)
    monkeypatch.setattr(tasks, "set_exam_stage", _stage)
    monkeypatch.setattr(tasks, "AsyncSessionLocal", lambda: _Session())

    await tasks._process_and_grade(1, 2)

    assert stages == [tasks.EXAM_STAGE_GRADING]
    assert tasks.EXAM_STAGE_GRADED not in stages


def test_the_concurrency_primitive_holds_no_module_level_loop_state():
    """A Semaphore created at import binds to the importing loop.

    Celery runs each task through `loop.run_until_complete`, which is not the
    loop that imported the module, so module-level asyncio state would fail in
    the worker and pass in every test that uses the server loop.
    """
    import backend.ai.concurrency as mod

    for name in dir(mod):
        value = getattr(mod, name)
        assert not isinstance(value, asyncio.Semaphore), f"module-level Semaphore: {name}"
        assert not isinstance(value, asyncio.Lock), f"module-level Lock: {name}"


# ---------------------------------------------------------------------------
# performance, against a fake provider only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_actually_reduces_wall_clock_time():
    """The mechanism does something. NOT a production performance claim.

    10 items, 200 ms each. Serial is ~2.0 s; a limit of N should approach
    2.0/N. The assertions are loose because CI timing is noisy -- the point is
    the shape, not the number.
    """
    async def worker(item):
        await asyncio.sleep(0.2)
        return item

    timings = {}
    for limit in (1, 2, 4):
        started = time.monotonic()
        outcomes = await run_bounded(list(range(10)), worker, limit=limit)
        timings[limit] = time.monotonic() - started
        assert all(o.ok for o in outcomes)

    assert timings[2] < timings[1] * 0.75, timings
    assert timings[4] < timings[2] * 0.75, timings
    print(
        "\nfake-provider benchmark (10 items x 200ms): "
        + ", ".join(f"concurrency={k}: {v:.2f}s" for k, v in sorted(timings.items()))
    )
