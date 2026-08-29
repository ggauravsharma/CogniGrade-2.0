# CogniGrade 2.0 — Project Context

Working memory for future sessions. Verify repository-dependent claims against
current code before relying on them.

Branch: `cognigrade-improvements` · Fork: `ggauravsharma/CogniGrade-2.0`

---

## Standing architecture rule

Gemini is the **current** AI provider, not the architecture. CogniGrade must stay
provider-agnostic and be able to adopt specialist models (segmentation, layout,
HTR, HMER, diagram, grading, verification) without a major rewrite. Do not
couple provider concepts into routes, schemas, the DB, or the frontend.

---

## Completed phases

### Repository audit
Full read-only audit. Key confirmed findings, none of which are fixed yet
except where noted:

- **C1 — FIXED** in Correctness Foundation v1. Was: `grade_question_with_diagram`
  uploaded only the *student's* images and spliced them into both the
  marking-scheme and the student-answer prompt slots, so diagram questions were
  graded against the student's own drawing.
- **C6 — FIXED** in Correctness Foundation v3. Was:
  `add_exam_result_internal` summed only non-NULL marks and then stamped
  `status="graded"`, so a failed grading silently scored zero.
- **C7** `marks_obtained` (×2) and `Question.max_marks` are `Integer` while the
  grader emits `float`. No Alembic migrations exist; schema comes from
  `Base.metadata.create_all`, which cannot alter existing columns.
- **C8** Answer crops are attributed to `current_user.id`; there is no
  `student_id` in the crop payload.
- **C10** `geminiAPI.py:1057` calls `db.query()` on an `AsyncSession` — that
  endpoint always fails.
- Crop pipeline discards page, bbox, sub-part, reading order and crossed-out
  state; question identity round-trips through pixels.
- `backend/routers/old/` is dead — it does not even compile.

### Security Foundation v1 — DONE
Exam / student / question / file surface.

- New `backend/auth/policies.py`: `ExamContext`, `load_exam_context`, and
  `require_exam_manager`, `require_exam_participant`,
  `require_self_or_exam_manager`, `require_question_in_exam`,
  `require_question_access_for_student`, plus imperative `assert_*` variants for
  handlers whose ids arrive in a request body.
- New `backend/auth/files.py`: replaced the public
  `app.mount("/uploads", StaticFiles(...))` with `/protected-files/...` routes
  addressed by **domain id, never by path**, plus a `_resolve_within_root`
  containment guard on paths coming out of the DB.
- Fixed three fully unauthenticated endpoints (`GET`/`POST /exams/{id}/stage`,
  `POST /exams/{id}/questions/parts`).
- Extracted `set_exam_stage()` so Celery no longer calls a route handler that
  carries `Depends` defaults.
- 47 tests.

### Security Foundation v2 — DONE
Classroom surface: classrooms, enrolments, people management, announcements,
assignments, submissions.

- Extended the same policy module with `ClassroomContext`,
  `load_classroom_context`, `require_classroom_participant` /
  `_manager` / `_owner`, `require_announcement_in_classroom`, and cross-resource
  helpers `assert_enrollment_manageable`, `assert_announcement_in_classroom`,
  `assert_assignment_access`, `assert_submission_access`.
- 83 tests (47 v1 + 36 v2), all passing.

### Correctness Foundation v1 — DONE
Diagram/table grading reference bug (audit C1).

- **Old behaviour:** one `uploaded_files` list held only the STUDENT's table and
  diagram images and was spliced into *both* the marking-scheme slot and the
  student-answer slot. Marking-scheme images were read from the DB and never
  uploaded. The grader compared a diagram against itself.
- **New contract:** `backend/grading/evidence.py` — a provider-neutral
  `GradingEvidence` / `ImageSet` structure with two separately-named sides
  (`reference_images`, `student_images`). There is no longer a single variable
  that could land in both slots. It holds plain paths and text only: no Gemini,
  FastAPI or SQLAlchemy types, so it is the shape a future `GradingProvider`
  input would take. It is NOT that interface yet.
- Two independent upload batches (`reference_uploads`, `student_uploads`),
  each image uploaded exactly once, order stable (text, tables, diagrams).
  Prompt slots are labelled `[REFERENCE / MARKING SCHEME IMAGES]` and
  `[STUDENT ANSWER IMAGES]`, emitted only when files exist.
- A missing reference image stays missing; it is never back-filled with the
  student's image.
- Asymmetry, deliberate: student *text* images are not re-attached because they
  already became `answer_text` upstream. Marking-scheme text images ARE
  attached, because the endpoint that would turn them into text
  (`process_marking_scheme_text_image`) is broken (C10).
- Crash paths on this route fixed at the same time: `len(None)` / iterating a
  None image field, `entry['img_path']` inside the upload error handler (path is
  a string), and blind unpacking of `asyncio.gather(return_exceptions=True)`
  results. The question is now resolved before any upload work.
- 114 tests (83 security + 31 grading). The two request-assembly regressions
  were verified to FAIL against the previous implementation.

### Correctness Foundation v2 — DONE
Structured grading result; the free-text parser is gone.

- **Old behaviour:** two byte-identical copies of a `Grade:` / `Reason:` text
  parser with `except ValueError: pass`. Every malformed response produced
  `grade = None`, the DB write was skipped, and the caller could not tell that
  apart from "not graded yet". Re-evaluation wrote that `None` straight over a
  student's existing marks. `float("nan")` parsed fine and PASSED the range
  check, because `nan < 0` and `nan > max` are both False.
- **New contract:** `backend/grading/result.py` — `GradingResult(score: float,
  reason: str, max_marks)`, `build_grading_result()` for deterministic
  validation, `parse_grading_response()` for strict JSON decoding, and
  `GradingResponseError(code, message, raw)`. Provider-neutral: no SDK,
  FastAPI or SQLAlchemy import (asserted by a token-level test).
- Score is a float so partial credit (0.5, 1.5, 2.25) is representable in the
  domain even though the DB columns are still Integer (C7 still open).
- Bounds enforced in code, not trusted to the model: `0 <= score <= max_marks`.
  An overshoot within `SCORE_EPSILON` (1e-6) snaps to max; anything beyond is
  an explicit failure, never a silent clamp. NaN/Inf/bool/non-numeric rejected.
- Provider request now asks for `response_mime_type="application/json"` and a
  one-line output instruction. No SDK-specific `response_schema` object is
  passed: the pinned google-generativeai 0.8.4 takes the mime type as a plain
  string, while `response_schema` expects a version-specific type. The decoder
  is strict about the JSON but tolerant of fences/prose wrapping.
- Both grading routes and all three re-evaluation call sites now share one
  conversion path. Failure returns `{"status": "grading_failed", "grade": None,
  "error_code": ...}`; re-evaluation checks status before writing and RESTORES
  the mark it nulled before re-grading, so a failed re-evaluation is
  non-destructive.
- 183 tests (114 + 69).
### Correctness Foundation v3 — DONE
Exam aggregation and grading-failure state (audit C6).

- **Old behaviour:** `add_exam_result_internal` did
  `sum(r.marks_obtained for r in responses if r.marks_obtained is not None)` and
  then set `status = "graded"` unconditionally. The filter looks defensive but
  is the bug: a question whose grading failed has NO mark by design (Correctness
  v2), was skipped by the sum, therefore contributed exactly zero, and the exam
  was still declared graded. A provider failure became a student's score.
- **New contract:** `backend/grading/aggregation.py` — `ExamResultStatus`,
  `AggregationResult(total_score, complete, graded_count, ungraded_question_ids,
  questions_without_response)` with derived `.status` / `.is_final`, and
  `aggregate_student_result()`. Provider-neutral: no SDK, FastAPI, SQLAlchemy or
  `backend.models` import (asserted by a token-level test). Reads rows by
  duck-typing `question_id` / `marks_obtained`.
- **Completeness rule:** a result is final when every question response that
  EXISTS carries a validated mark. `marks_obtained = 0` is a grade and counts;
  only `None` blocks. A question with NO response row is reported in
  `questions_without_response` but does NOT block — the database cannot tell
  "the student skipped it" from "grading failed", and blocking would make any
  exam with an unattempted question permanently unfinalisable.
- **New status `"grading_incomplete"`.** `ExamResult.status` is a Text column,
  so no migration. It is recomputed from facts on every aggregation, never
  latched: filling the missing mark (`drop_question`, `give_full_marks`, a
  successful re-grade — all of which already call `add_exam_result_internal`)
  promotes it back to `graded`, and a later failure demotes it again.
- `graded_at` is set ONLY on a final result. A timestamp asserts "this is the
  student's grade"; an incomplete run has not earned one.
- Partial totals are preserved and reported, never presented as final:
  * student `submission_status` returns `is_final`; `student-edit.htm` gained a
    `grading_incomplete` branch that shows a non-final notice and neither opens
    the script page nor re-enters the crop flow;
  * the `classes.py` classwork card returns `score: None` plus `status` and
    `is_final` when not final, so a partial total cannot reach a student as
    their score;
  * the professor dashboard reports per-student `status` / `is_final`, counts
    only final results in `grading_progress`, EXCLUDES non-final totals from
    `marks_distribution` (reporting `excluded_from_distribution`), flags the row
    "partial", and adds a Finalised column to the CSV export.
- **Also fixed here:** `GET /exams/{id}/submission_status` was gated on
  `require_exam_manager` by Security Foundation v1, which 403'd exactly the
  students it exists to serve (it only ever returns the caller's own row). Now
  `require_exam_participant`, with a regression test.
- 206 tests (183 + 23). Five of the new tests were verified to FAIL against
  bb536cd.

---

## Authorization model

Two capability ladders, both derived from the real domain model. `is_professor`
alone is **never** sufficient for any specific resource.

```
EXAM        manager      = classroom owner | exam author | accepted professor/TA enrolment
            participant  = manager | accepted student enrolment

CLASSROOM   owner        = Classroom.owner_id            (ownership-sensitive only)
            manager      = owner | accepted professor/TA enrolment
            participant  = manager | accepted student enrolment
```

`owner` is deliberately narrower than `manager`: promoting a member to TA grants
them manager rights, so promotion must not itself be a manager-level action.

Status semantics: **401** anonymous · **403** authenticated but forbidden ·
**404** resource absent, or a child resource that does not belong to the parent
named in the path (avoids cross-classroom existence leaks).

Cross-resource rule: when a route carries more than one id, the parent is
resolved **from the child resource itself**, so authorizing against classroom A
can never permit acting on a resource in classroom B.

---

## Known issues found but deliberately NOT fixed

These were discovered while implementing security phases and are out of scope
until their own remediation phase.

- `classes.py` has **17 handlers** wrapping the whole body in
  `except Exception -> HTTPException(500)`, which converts deliberate 404/403
  responses into 500. Authorization dependencies are unaffected (they run before
  the body), but in-body checks report the wrong status.
- `peopleManagement.get_class_people` returns 500 for everyone: it reads the
  lazy relationship `classroom.owner.full_name` in async context.
- `announcements.get_class_announcements` returns 500: `db.query()` on an
  `AsyncSession` (same class of bug as `geminiAPI.py:1057`).
- `peopleManagement.remove_student` calls `db.delete(...)` without `await`, so
  the removal does not happen while the endpoint reports success. That route is
  in any case shadowed — see below.
- Still open in the grading path after Correctness v3: **C7** integer marks
  columns (needs Alembic — `create_all` cannot alter them, so a fractional
  score is still truncated on write); grading is serial, one provider call
  per question; uploaded Gemini files are never deleted (`upload_file` used,
  `delete_file` never) — a diagram/table call uploads reference + student
  images per question and none are cleaned up.
- `grade_exam_logic` returns `graded_count` / `failed_count` /
  `failed_questions`. Aggregation does NOT read that return value: it derives
  completeness from the persisted marks instead, so a crashed or partially
  executed grading run is caught just as well as a politely reported one. The
  return value is still useful for surfacing WHY a question failed, which no
  UI does yet.
- `tasks.py` advances the exam to stage 7 after grading regardless of outcome.
  That is correct as it stands — stage 7 is "grading started", an exam-wide
  workflow marker, and result release is gated per student by
  `ExamResult.status` — but it means the exam stage alone never signals
  trouble.
- **Four duplicate route paths.** The first router registered in `main.py` wins:
  `POST /classes/join-class` (classes.py wins), `POST` and `PUT`
  `/classes/{class_id}/announcements[/{id}]` (classes.py wins),
  `POST /enrollments/{enrollment_id}/remove` (enrollments.py wins). Both copies
  of each are secured, since include order could change.

---

## Remaining security risks

- `/profile_pictures` is still a public `StaticFiles` mount.
- CORS `allow_origins=["*"]` with `allow_credentials=True`; `SECRET_KEY` falls
  back to a random value; `logging.DEBUG` globally.
- No CSRF tokens on cookie-authenticated mutations.
- Assignment submission files have no protected-file route.
- Gemini uploads are never deleted (`upload_file` used, `delete_file` never).

---

## Testing

`backend/tests/`, run with `.venv-test`. In-memory SQLite, real models and
routers, authentication shimmed to an `X-Test-User` header so the suite tests
authorization rather than token signing. The Gemini SDK is stubbed only when the
real package is absent, and the stub raises if any test reaches a model.

```
.venv-test/Scripts/python.exe -m pytest -q      # 206 passed
```

`backend/requirements-dev.txt` holds test-only dependencies; production
`requirements.txt` is untouched.

Caveat: SQLite, not Postgres. Nothing here is integration-tested against the
real database, RabbitMQ, Celery, or Gemini.

---

## Experiments (isolated, not production)

`experiments/` holds three provider-evaluation harnesses that are the reference
for the future provider-agnostic schema: `segmentation-v1`, `hierarchy-v1`
(question tree + OR-choice + marking-scheme alignment, scored clean on its
stress set), `seg-v1.1` and `seg-v11-lite` (recall hardening, page detection and
rectification for photographed sheets). Their `results/` and
`experiment-data/` are gitignored and contain student data — never commit them.

---

## Next approved phase

Not yet approved. Recommended next: **C7 — numeric mark columns**, which now
blocks the rest of the grading path. `GradingResult.score` is a float and
aggregation sums floats, but `QuestionResponse.marks_obtained`,
`ExamResult.marks_obtained` and `Question.max_marks` are still `Integer`, so
half marks are truncated on write. Fixing it requires introducing Alembic, since
the schema comes from `Base.metadata.create_all`, which cannot alter a column.
That is a migration-infrastructure phase, not a one-line change.

After that: nothing in the UI explains WHY a question failed grading.
`grade_exam_logic` already returns `failed_questions`, and the professor
dashboard now has a place to put it.
