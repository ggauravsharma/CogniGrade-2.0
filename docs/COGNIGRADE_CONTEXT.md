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
- **C7 — FIXED** in Correctness Foundation v4. Was: `marks_obtained` (×2) and
  `Question.max_marks` were `Integer` while the grader emits `float`, and no
  Alembic migrations existed — schema came from `Base.metadata.create_all`,
  which cannot alter an existing column.
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

### Correctness Foundation v4 — DONE
Alembic foundation and fractional marks (audit C7).

- **Old behaviour:** every score column was `Integer`. `GradingResult.score` has
  been a float since Correctness v2 specifically so partial credit is
  representable, and aggregation sums floats — but the write truncated. A 1.5
  became 1 on PostgreSQL, i.e. half a mark taken off the student, silently.
  `create_all` could never fix it: it creates missing tables and nothing else.

- **Numeric decision:** `NUMERIC(7, 2)` — exact decimal, not binary float,
  because a transcript is an exact record and `REAL` cannot hold 0.1. Scale 2
  covers halves, quarters and hundredths (percentage-derived rubrics); it does
  not represent exact thirds, which is how such a scheme is written down anyway.
  Precision 7 (±99999.99) is two orders of magnitude above any real exam total.

- **New modules:**
  * `backend/grading/marks.py` — provider-neutral `to_decimal` / `to_number`,
    `InvalidMarkError(code, ...)`, `MARKS_PRECISION` / `MARKS_SCALE`. No SDK,
    FastAPI, SQLAlchemy or `backend.models` import (asserted by an AST-level
    test). Accepts int/float/Decimal/numeric-str; rejects bool, NaN, Inf,
    non-numeric and out-of-range; `None` and empty string stay `None`.
  * `backend/models/numeric.py` — `Marks`, a `TypeDecorator` over
    `Numeric(7,2)`. **This is the whole Decimal/float policy:** write quantises
    to `Decimal`, read returns `float`. Nothing else in the codebase converts,
    so `JSONResponse` and every existing float arithmetic keep working, and
    float drift (0.1+0.2) is quantised away at the write boundary instead of
    accumulating in the database.
  * `backend/db_bootstrap.py` — the startup schema decision, below.

- **Columns migrated (6):** `QuestionResponse.marks_obtained`,
  `ExamResult.marks_obtained`, `Question.max_marks`, `Submission.grade`,
  `Assignment.points_possible`, `Exam.points_possible`. The two
  `points_possible` maxima are included deliberately: leaving them Integer
  would make an exam out of 37.5 unrepresentable while its questions could hold
  fractions. Names stay domain names — never `gemini_score`.

- **Alembic:** `backend/alembic.ini` (inside `backend/` so it ships in the
  image; `script_location = %(here)s/migrations`, no `sqlalchemy.url`, so no
  credential is committed) and `backend/migrations/env.py`, which takes metadata
  from `backend.database.Base` after importing every model module, takes the URL
  from `-x url=` then Config then `ALEMBIC_DATABASE_URL` then
  `settings.DATABASE_URL`, drives async engines through `run_sync`, and uses
  batch mode on SQLite only.

- **Migration strategy — baseline + stamp.** `0001_baseline` reproduces the
  pre-Alembic schema exactly, Integer marks columns and all;
  `0002_fractional_marks` alters the six columns. Fresh DB: `upgrade head`.
  Existing DB: back up, `stamp 0001`, then `upgrade head` (only 0002 runs).
  0001 must NEVER be edited to track later models — that would make the stamp
  a lie. A test asserts it still declares `sa.Integer()`.

- **Downgrade is guarded, not silent.** `downgrade 0002` counts non-integral
  values in every affected column and raises, naming table/column/row counts,
  rather than rounding a student's mark. With whole-number data it proceeds and
  is loss-free.

- **`create_all` is now three-way** (`db_bootstrap.bootstrap_schema`):
  `alembic_version` present -> do nothing, Alembic owns it; no tables at all ->
  create from models AND stamp head, so the two mechanisms stay consistent;
  tables but no `alembic_version` -> pre-Alembic database, warn loudly with the
  adoption commands and touch nothing. The stamp degrades to a warning if
  alembic is not importable, so startup never hard-requires migration tooling.

- **Application fixes:** manual marks now go through
  `backend/utils/marks_input.parse_mark_input` (the UI posts strings; a bad
  value is a 400 naming the field instead of a driver error at flush time);
  `update_student_response` no longer nulls a mark when the field is absent;
  the stats histogram derives its bucket count instead of assuming
  `points_possible` is an int; `GradeSubmission.grade`,
  `AssignmentCreate.max_marks`, the `points_possible` Form field and
  `UpdatePartLabels.maxMarks` became floats; the marks inputs in
  `exam-stats.htm` / `assignment.htm` gained `step="any"` and `parseInt`
  became `parseFloat`.

- **C6 unchanged and re-asserted:** `0.0` is a mark, `None` is a missing
  grading result, `grading_incomplete` still blocks finalisation. Tests cover
  fractional zero, a fractional partial total that stays non-final, and a
  failed re-evaluation restoring a fractional mark.

- 283 tests (206 + 77); 285 after the PostgreSQL verification below added
  two bootstrap regression tests.

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
- Still open in the grading path after Correctness v4: grading is serial, one
  provider call per question; uploaded Gemini files are never deleted
  (`upload_file` used, `delete_file` never) — a diagram/table call uploads
  reference + student images per question and none are cleaned up.
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
.venv-test/Scripts/python.exe -m pytest -q      # 285 passed
```

`backend/requirements-dev.txt` holds test-only dependencies; production
`requirements.txt` is untouched.

Caveat: SQLite, not Postgres. Nothing here is integration-tested against the
real database, RabbitMQ, Celery, or Gemini.

That caveat bites hardest on C7. **SQLite has dynamic typing** and will store
1.5 in a column declared INTEGER, so the end-to-end fractional tests pass even
against the old schema. Inside the suite, what pins C7 is therefore static, not
behavioural: the model columns are asserted to be `Marks`, their PostgreSQL DDL
is asserted to compile to `NUMERIC(7, 2)`, and the migration's offline
PostgreSQL output is asserted to contain the six `ALTER COLUMN ... TYPE
NUMERIC(7, 2)` statements with no table drop or recreate.

The behavioural half was closed separately — see **PostgreSQL runtime
verification** below. Verification status:

```
UNIT TESTED                        marks normalisation, aggregation, routes
MIGRATION FILE VERIFIED            graph, chain, model/migration agreement
STATICALLY VERIFIED                PostgreSQL DDL, offline `upgrade --sql`
SQLITE MIGRATION TESTED            upgrade, data preservation, guarded downgrade
POSTGRES MIGRATION TESTED          fresh + adoption path on PostgreSQL 15.19
POSTGRES DATA-PRESERVATION TESTED  integer marks -> x.00, NULL preserved
POSTGRES FRACTIONAL W/R TESTED     0.5 / 1.5 / 2.25 through the ORM and routes
```

---

## PostgreSQL runtime verification (C7)

Run against a disposable **PostgreSQL 15.19** container (`postgres:15`, the
image `docker-compose.yml` uses), on a throwaway port with ephemeral storage.
No student data, no production database, nothing committed.

The pre-Alembic schema was not simulated: a git worktree at `f6057cc` (the last
commit before C7) ran the *old* models' `Base.metadata.create_all` against an
empty database, producing a genuine legacy schema with six `integer` score
columns and no `alembic_version`.

**The defect reproduced.** On that legacy schema PostgreSQL silently rounded:
`1.5 -> 2`, `2.25 -> 2`, `0.5 -> 1`. C7 was not theoretical.

**Fresh path.** `upgrade head` on an empty database: both revisions ran, all 16
tables plus `alembic_version` created, revision `0002`, all six columns
`numeric(7,2)`, and an autogenerate comparison against the live database
reported no drift.

**Adoption path.** On the legacy database, `stamp 0001` applied no DDL and
`upgrade head` ran only 0002. Values after: `3 -> 3.00`, `0 -> 0.00`,
`NULL -> NULL`, `5 -> 5.00`, `4 -> 4.00`, `7 -> 7.00`, `100 -> 100.00`,
`80 -> 80.00`. Row counts unchanged; no drift.

**Fractional behaviour.** 0.5 / 1.5 / 2.25 / 0.0 written through the real
models return as those floats and sit on disk as `0.50` / `1.50` / `2.25` /
`0.00`. `0.1 + 0.2` is quantised to `0.30` at the write boundary. Aggregation
totals 3.75 and, on a larger set, the application's float total matched
PostgreSQL's own exact `SUM()` over the NUMERIC column to the cent. The manual
`edit_marks` and `update_student_response` routes carried a fractional mark
from an HTTP string through to `numeric(7,2)` and back out of the evaluation
endpoint; a non-numeric mark was a 400. C6 held throughout: a stored zero read
back as `0.0` (never `None`), a `NULL` mark read back as `None` and kept the
result `grading_incomplete`.

**Downgrade.** With fractional marks present, `downgrade 0001` refused, naming
the offending columns and row counts, and left the schema, the revision and
every value untouched. With whole-number data it succeeded and was loss-free
(`3.00 -> 3`, `0.00 -> 0`, `NULL -> NULL`), and re-upgrading returned to head.

**Bug found and fixed.** `db_bootstrap` read `Base.metadata` without importing
the model modules. Declaring a model registers its table as an import side
effect and nothing else does, so a caller that had not already imported
`backend.models` got an EMPTY metadata: `create_all` created nothing and the
fresh-database branch stamped head anyway, leaving a schema-less database that
Alembic believed was fully migrated and would never repair. `main.py` is safe
only by accident of import order (it loads the routers first). Fixed by
importing the model packages in `db_bootstrap` (as `migrations/env.py` already
did) plus a guard that refuses to stamp when no models are registered. Two
regression tests, both verified to fail against the previous module — one
spawns a clean interpreter, because this suite cannot see the failure
in-process once conftest has imported the models.

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

Not yet approved. Recommended next: **surface WHY a question failed grading.**
`grade_exam_logic` already returns `graded_count` / `failed_count` /
`failed_questions`, and nothing reads it. Since Correctness v3 the professor
dashboard reports a `grading_incomplete` result and flags the row "partial",
but it cannot say which question failed or why, so the only recovery is to
re-run grading blindly. The data exists; it needs to be persisted or passed
through and rendered.
