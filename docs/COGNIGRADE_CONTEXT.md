# CogniGrade 2.0 — Project Context

Working memory for future sessions. Verify repository-dependent claims against
current code before relying on them.

Branch: `cognigrade-improvements` · Fork: `ggauravsharma/CogniGrade-2.0`

---

## What CogniGrade is

An **AI-first** automated assessment system. The normal path is fully
automatic:

```
question paper / marking scheme / answer script uploaded
  → the model does the document understanding, recognition and question mapping
  → automatic grading
  → aggregation
  → results
```

A human is an **exception path only**: model failure, model uncertainty, a
detected anomaly, or a teacher's correction/override.

CogniGrade is therefore **not** an annotation platform, not a LabelMe-style
labelling tool, and not a dataset-labelling workflow. The region API and the
read-only overlay exist to carry *structured model output* into grading, not to
put a drawing task in front of a teacher. **Do not build a manual region editor
as the normal workflow.** Manual endpoints stay as an exception/correction
seam; they must never become the route an ordinary exam travels.

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

### Backend Reliability v1 — DONE
Grading-failure visibility, and the runtime bugs that would have broken a demo.

A correct grading algorithm is not enough if an announcement page 500s, a
deletion lies about succeeding, a 403 arrives as a 500, or a professor is told
grading failed but not where. All of the following were confirmed at HEAD
before being changed.

**Grading failure visibility.** `grade_exam_logic` had always computed
`failed_questions`, and `tasks.py` had always discarded the return value, so
`grading_incomplete` was a dead end: no question, no reason, no recovery but a
blind re-run.
* New `backend/grading/failure.py` — provider-neutral `GradingFailure`,
  `FAILURE_MESSAGES` (code -> one safe sentence), `describe()`,
  `collect_failures()`. No SDK, FastAPI, SQLAlchemy or `backend.models` import
  (AST-asserted). No message names a provider (asserted).
* New column `QuestionResponse.grading_error_code` (Text, nullable), Alembic
  revision **0003**. One nullable column, deliberately: not a job table, not an
  event ledger. Only the CODE is stored — the raw provider response is logged
  and never persisted, so it cannot reach a classroom UI.
* Exposed on two surfaces that already existed, no new endpoints:
  `GET /exams/{id}/stats` (manager-only) gains `grading_failures` and
  `failed_question_labels` per student, built in ONE query for the whole exam;
  `GET /exam/{id}/student-evaluation/{sid}` gains `grading_error_code` /
  `grading_error` **only when `ctx.is_manager`** — that route is
  self-or-manager, and a student gets their marks without the diagnostics.
* `grade_exam_logic` no longer aborts the paper when one question raises: the
  question is recorded as failed and the run continues.

**Failure lifecycle.** The code is cleared in the same transaction as any
write that produces a valid mark — provider success, manual edit,
`update_student_response`, all three re-evaluation call sites, `drop_question`,
`give_full_marks`. A stale failure therefore cannot outlive the failure it
described, which is what makes retries self-healing without an attempt log.
`marks_obtained = 0` is a grade and is never listed as a failure (C6 holds).

**Announcements (`db.query` on an AsyncSession).** `get_class_announcements`
returned 500 for everyone; so did `delete_announcement`, which additionally
called `db.delete()` and `db.rollback()` without awaiting them. Both converted
to async `select`; author names now resolve in one query instead of one per
row.

**People management (async lazy load).** `get_class_people` returned 500 for
everyone: `classroom.owner.full_name` on a `ctx.classroom` loaded by a plain
select. The owner name is now SELECTed; `e.student.full_name` — the same bug
two lines down — is covered by `selectinload(Enrollment.student)`. No global
lazy-loading configuration was changed.

**Deletions that lied.** `AsyncSession.delete` is a coroutine; unawaited it
schedules nothing while the endpoint reports success. Fixed in
`announcements.delete_announcement` and `classes.delete_comment`. The
`peopleManagement.remove_student` copy that had this bug was dead (below).
STILL OPEN: `user_routes.delete_account` has six unawaited deletes AND reads
lazy relationships in async context — out of this phase's scope, reported.

**Duplicate routes — now zero.** All four shadowed handlers removed, keeping
the implementation FastAPI actually served, so no observable API changed:
`enrollments.join_class`, `announcements.create_announcement`,
`announcements.update_announcement`, `peopleManagement.remove_student`. Two
behaviour differences were dead, not lost, and are recorded below. A test
asserts no duplicate (method, path) registration remains.

**HTTPException swallowing — now zero.** An AST audit found 17 try-blocks that
raise HTTPException and catch bare `Exception` first, turning a deliberate
403/404 into a 500. Two were fixed inside the grading routes and 15 guarded
with `except HTTPException: raise` (classes.py 13, exams.py 1, geminiAPI.py 1).
Genuine faults still surface as server errors.

**Also fixed:** `process_marking_scheme_text_image` used `db.query` on an
AsyncSession (audit C10) — the endpoint failed on its first line. Converted to
async select; it still requires a live provider call to exercise end to end, so
it is NOT verified beyond compiling and no longer crashing on entry.

- 320 tests (285 + 35).

### Backend Reliability v2 — DONE
Account deletion (`POST /delete-account`).

The one endpoint that makes a privacy promise, and it kept none of it.

**Confirmed at HEAD before changing anything.** The handler walked four LAZY
relationships (`answer_scripts`, `question_responses`, `enrollments`,
`received_notifications`) in async context, so it raised on its first line of
real work — the observed response was
`500 … lazy load operation of attribute 'answer_scripts' cannot proceed`.
Behind that sat six `db.delete(...)` calls and a `db.rollback()` with no
`await`; `AsyncSession.delete` is a coroutine, so each one built an object and
threw it away (pytest reported `coroutine 'AsyncSession.rollback' was never
awaited` directly). Fixing only the lazy loads would therefore have produced
the WORSE failure: a 200 for an account that was never deleted.

**The fix is one Core DELETE.** `await db.delete(user)` would still have been
wrong: most `User` relationships lack `passive_deletes=True`, so the ORM would
load every child at flush time to null its foreign key — the same lazy IO,
moved. `await db.execute(sa_delete(User).where(User.id == user_id))` bypasses
ORM relationship processing entirely. The instance is expunged first so nothing
tries to flush it.

**The database deletes the rest.** All 17 foreign keys pointing at `users.id`
are declared `ON DELETE CASCADE`, so the schema already states the policy and
duplicating it in Python would mean maintaining a second, divergent copy.

**SQLite now enforces foreign keys** (`backend/database.py`, a `connect`
listener on the `Engine` class issuing `PRAGMA foreign_keys=ON`). SQLite ignores
every FK constraint unless that pragma is set per connection, so on dev and
test databases every `ondelete=` in this schema had been a dead letter and the
cascade could not be tested at all. Registered on the class so the app, Alembic
and test engines agree.

**That change bit immediately, and is handled.** SQLite performs an implicit
`DELETE FROM` before a `DROP TABLE`, which fires cascades — so alembic batch
mode rebuilding `questions` silently emptied `question_responses`. Two
migration tests caught it. `migrations/env.py` now attaches its own `connect`
listener turning the pragma back OFF for migration connections only (the
documented practice; the pragma is a no-op inside a transaction, so it must be
set at connect time).

**Transaction order corrected.** Password check → Core DELETE → commit →
*then* remove the profile picture. The old code deleted the picture BEFORE the
transaction, so a failed deletion still destroyed it. File removal is best
effort and cannot turn a completed deletion into an error. On any exception:
`await db.rollback()` and a 500 whose body no longer echoes `str(e)` (that
leaked driver text, which can carry connection details).

**Retention policy: unchanged and NOT invented here.** The old code set
`notification.sender_id = None`, evidently intending to preserve notifications
the user had sent — but the FK is `ON DELETE CASCADE`, so the schema deletes
them. Since that code never ran, there is no established behaviour to preserve;
the schema is followed and the discrepancy is recorded below.

**Blast radius, recorded not chosen:** `classrooms.owner_id` cascades, so
deleting a professor's account destroys their classrooms and every exam,
assignment and result inside them. Existing schema policy, now covered by a
test so it cannot surprise anyone silently.

**Static guarantee:** an AST scan of all live backend modules finds ZERO
unawaited `AsyncSession` calls (`delete`/`commit`/`rollback`/`execute`/
`refresh`/`flush`/`close`). A test keeps it that way.

- 339 tests (320 + 19). Sixteen of the nineteen were verified to fail against
  the previous handler; the three that passed only assert the response shape,
  which was never the broken part.

### AI Platform Foundation v1 — DONE
A provider-neutral service boundary, and Gemini made operationally safe.

`geminiAPI.py` held the SDK, the API keys, the model objects, five upload call
sites, every prompt, the generation config, the parsing and the orchestration.
Replacing Gemini meant rewriting a router.

**The new shape.**

```
route / orchestration
        ↓  backend/ai/services.py      task-shaped, provider-neutral
        ↓  backend/ai/contracts.py     ProviderRequest / ProviderResponse
        ↓  backend/ai/providers/       one adapter per provider
     Gemini today
```

Only `backend/ai/providers/` may import a vendor SDK. Asserted by a test that
walks every module in `backend/ai/` and every router.

**Five tasks, named** (`AITask`): `document_extraction`, `label_extraction`,
`answer_recognition`, `marking_scheme_recognition`, `grading`. Not a generic
`generate(prompt)` — the task name is what configuration, model selection,
prompt versioning and telemetry are keyed by. Deliberately the current five;
`SEGMENTATION`, `STRUCTURE`, `VERIFICATION` arrive when they become real.

**Configuration is per task**, environment only, no DB:
`CG_AI__<TASK>__MODEL`, `__TIMEOUT_SECONDS`, `__MAX_RETRIES`, `__PROVIDER`,
`__TEMPERATURE`, with a `CG_AI__<FIELD>` global fallback. All five tasks point
at `gemini-2.0-flash` today, unchanged — moving the model is a benchmarking
decision, not a refactor. A malformed override falls back to the default rather
than taking grading down.

**Retry** (`backend/ai/retry.py`): bounded, full-jitter exponential backoff,
per-step ceiling. Retryability is declared on the error CLASS, so it is decided
once. Retried: rate limit, temporary, timeout. Never retried: authentication,
invalid request, unusable response. Default 2 retries (3 attempts).

**Timeout:** `request_options={"timeout": N}` is passed to `generate_content`,
with a `TypeError` fallback for SDKs that reject it, wrapped in
`asyncio.wait_for(budget + 5)` as a backstop. Honest limitation: the wait_for
releases the caller but cannot kill the worker thread, and `upload_file` in
0.8.4 takes no deadline of its own.

**File lifecycle — the leak is closed.** `upload_file` had five call sites and
`delete_file` none, so every graded diagram question left provider files behind
forever. Uploads now happen only inside the adapter, and deletion is in a
`finally`: it runs on success, on provider failure and on timeout. Cleanup
failure is logged and swallowed — losing a remote temp file must never destroy
a grade that was produced. Local files are never touched. Within one call each
DISTINCT path uploads once (dedup by resolved path, so it can never make one
C1 image side stand in for the other).

**Error taxonomy** (`backend/ai/errors.py`): `ProviderRateLimitError`,
`ProviderTemporaryError`, `ProviderTimeoutError`, `ProviderAuthenticationError`,
`ProviderInvalidRequestError`, `ProviderResponseError`. SDK exceptions are
classified by type name and message text rather than by importing
`google.api_core`, whose classes move between versions. An unrecognised failure
is treated as temporary — a bounded retry is cheaper than failing a whole exam
on one odd transport error. A provider failure now reaches the grading route as
a `grading_failed` with the CATEGORY as its `grading_error_code`, so Reliability
v1's failure visibility now covers transport failures too, not just validation
ones.

**Prompts** moved to `backend/ai/prompts/` under versions (`grading/v1`,
`answer_recognition/v1`, ...). Wording is verbatim — this phase is architecture,
and rewording a grading prompt changes marks. The version travels into telemetry
so a later experiment log can say which prompt produced a result.

**Ordered prompt parts.** `ProviderRequest.parts` interleaves `TextPart` and
`FilePart`. Diagram grading positions the marking-scheme images immediately
after the marking-scheme text and the student's images immediately after the
student's answer; flattening to "all files then all text" would change what the
model is asked. The adapter preserves order exactly (tested).

**Telemetry** (`backend/ai/telemetry.py`): one structured line per invocation —
task, provider, model, prompt_version, duration_ms, attempts, success,
error_category, file count, exam/student/question ids. Never the key, the
student's answer, the marking scheme, file contents or the raw response;
asserted by a test.

**API keys — the rotation was a no-op.** `genai.configure()` sets a
MODULE-GLOBAL key and `GenerativeModel` resolves one at call time, so every
model in the old list used whichever key was configured LAST and `get_model()`
rotating every 15 calls rotated nothing. The adapter now uses ONE key (the
first) and warns loudly when more are configured. Behaviour change: the key in
use moves from last to first. Real rotation needs per-call client binding,
which the legacy SDK does not offer cleanly.

**Concurrency-ready.** The module-global `call_count` guarded by a threading
lock AND an asyncio lock is gone. No shared mutable per-request state remains,
so the later bounded-concurrency phase has nothing to unpick.

**SDK unchanged.** Still `google-generativeai==0.8.4`. Nothing in this phase
required migrating to `google-genai`, and the adapter now makes that migration
a single-file change. What would justify it: a real request deadline on uploads,
and per-call client binding for genuine key rotation.

- 402 tests (339 + 63). The nine pre-existing grading tests were rewired from
  SDK-level stubs to a recording PROVIDER, which exercises strictly more real
  code (prompt building, request assembly, retry, telemetry) and keeps working
  when the provider does not.

### AI Platform Foundation v2 — DONE
Bounded concurrency for exam grading.

`grade_exam_logic` awaited one provider call per question in a loop, so a
40-question paper was 40 sequential round trips.

**Why a bare `gather` would have been a bug.** The loop read the database,
called the provider and wrote the result per question on ONE `AsyncSession`.
An `AsyncSession` is not safe for concurrent use — two coroutines sharing one
interleave on the same connection. Concurrency here needed a phase split, not
a wrapper.

**Three phases:**

```
1. LOAD     serial, one session   read questions + responses, build every prompt
2. GRADE    concurrent, NO session provider calls only, semaphore-bounded
3. PERSIST  serial, one session   write outcomes in QUESTION ORDER
```

Phase 1 ends holding plain dicts — no ORM instance survives into phase 2, so
the session is never touched by two coroutines at once. `_build_diagram_prompt_parts`
was extracted from the route body (verbatim, including its unusual 8-space
indentation, because the prompt literals are multi-line f-strings whose
continuation lines are part of the string) so both the route and the exam run
build the same prompt.

**`backend/ai/concurrency.py`** — `run_bounded(items, worker, limit)`. Returns
one `Outcome` per item in INPUT order, never completion order. Failures are
captured per item, so one question failing cannot cancel its siblings the way
`asyncio.gather`'s default would. `limit <= 1` runs a plain sequential loop,
which is what makes `max_concurrency = 1` an exact restoration rather than an
approximation. The semaphore is created PER CALL, never at module scope,
because Celery drives this through `loop.run_until_complete` rather than a
long-lived server loop.

**Configuration:** `TaskSettings.max_concurrency`, per task, via
`CG_AI__<TASK>__MAX_CONCURRENCY`. Grading defaults to **3** — Gemini's free
tier is roughly 15 requests/minute for flash models and a grading call takes
seconds, so sequential grading was already near that ceiling; 3 is a real
speed-up the full-jitter retry can absorb 429s around. Values `<= 0` are
clamped to 1 rather than deadlocking. **Setting it to 1 is the emergency
quota control** and needs no deploy.

**Recognition was already concurrent, and unbounded.** Two pre-existing bare
`asyncio.gather` calls put EVERY image batch in flight at once — a 60-image
paper opened twelve simultaneous provider calls. Those now go through
`run_bounded` at a limit of 3. This phase capped concurrency there; it did not
introduce it. Document and label extraction stay at 1 (one file, one call).
An AST-based test asserts no bare `asyncio.gather` remains in the router.

**Retries are not multiplied.** The exam layer adds no retry of its own and
relies on the service contract. Asserted: one permanently failing question
produces exactly `max_retries + 1` provider calls, not `(max_retries + 1)²`.

**Aggregation still runs after everything.** `backend/tasks.py` is unchanged:
recognise → grade → `add_exam_result_internal` → stage. A test replaces each
step with a recorder and asserts grading FINISHES before aggregation starts.
C6 derives completeness from the persisted marks, so provider completion order
cannot affect it.

**Celery bridge unchanged, deliberately.** `tasks.py` still uses
`get_event_loop().run_until_complete(...)`. Switching to `asyncio.run` would
close the loop after each task, and the SQLAlchemy async engine's pool holds
connections bound to that loop — reusing the engine on a later task would then
fail. Known limitation: `asyncio.get_event_loop()` with no running loop raises
on Python 3.12+; production runs 3.11 (`python:3.11-slim`), where it still
creates one. That is a latent upgrade blocker, recorded not fixed.

**Fake-provider benchmark** (10 items × 200 ms, no quota used):

```
concurrency=1   2.04s
concurrency=2   1.01s
concurrency=4   0.61s
```

- 436 tests (402 + 34).

### Live validation v1 — DONE (findings, not architecture)
Real PostgreSQL + real Gemini, one prepared exam, three runs.

Ran against a **disposable copy** of the development database (`pg_dump` of the
running stack restored into a separate throwaway container). The live
`classroom` database was never written to. Workload: the developer's own test
exam — 39 questions, 33 with student evidence, 6 without.

**FINDING 1 — the configured model had been RETIRED.** Every call returned

```
404 This model models/gemini-2.0-flash is no longer available.
    Please update your code to use models/gemini-3.6-flash
```

CogniGrade could not grade anything at all, at any concurrency. `DEFAULT_MODEL`
is now `gemini-3.6-flash`, the replacement the provider itself named, verified
with one live call. `RETIRED_MODELS` plus a regression test stop a withdrawn
model becoming the default again. **Grading QUALITY on the new model is
unbenchmarked** — the retired model made any comparison impossible.

**FINDING 2 — the deployed container's API key does not authenticate.**
`GEMINI_API_KEY_1` inside the running `portal_backend` is rejected with
`401 … Expected OAuth 2 access token`. The key in the repository-root `.env`
works, but is named `GEMINI_API_KEY` while the code has always read
`GEMINI_API_KEY_1`, so that file alone would configure nothing. Both are the
newer `AQ.`-format keys, not legacy `AIza`. **FIXED** in MVP Deployment v1:
`GEMINI_API_KEY` is now the canonical variable, with the numbered form kept as
an explicit legacy fallback.

**FINDING 3 — quota, not concurrency, is the binding constraint.** Three
consecutive runs of the same workload:

```
run          conc  wall_s  graded  failed  attempts  429-retries  lost to 429
A (first)      1   223.4     16      23       68          51           16
B (second)     3    31.2      3      36       93          90           30
C (third)      1   221.9      0      39       99          98           33
```

Even the FIRST sequential run lost 16 of 33 questions to rate limiting. The
graded count then fell 16 → 3 → 0 across runs regardless of concurrency, so the
quota depletes monotonically and **the concurrency variable cannot be isolated**
on this key. Run B's apparent 7× speed-up is an artefact: it finished quickly by
failing quickly.

**FINDING 3a — the exact limit, from the provider's own error metadata.**
A follow-up probe read the structured quota block returned with the 429:

```
quota_id     GenerateRequestsPerDayPerProjectPerModel-FreeTier
quota_metric generativelanguage.googleapis.com/generate_content_free_tier_requests
dimensions   model=gemini-3.6-flash, location=global
quota_value  20
```

**20 requests per DAY, per project, per model, free tier.** The bucket is scoped
to (project x model), so it is both project-level and model-level: a different
model gets its own fresh 20/day. But a 33-question paper needs 33 calls, so
**no model choice can complete one paper on the free tier** — this needs billing
on the project, not a configuration change and not more API keys.

Model availability at the time of testing, same key:

```
gemini-2.0-flash   404 retired
gemini-2.5-flash   404 "no longer available to new users"
gemini-3.6-flash   works; daily quota exhausted (the provider's recommended model)
gemini-3.7-flash   authenticates, NOT quota-blocked, but 503 "high demand" on
                   every attempt -- unusable, for a different reason
```

**Concurrency verdict: INSUFFICIENT EVIDENCE.** `max_concurrency` stays at 3,
unvalidated, with `CG_AI__GRADING__MAX_CONCURRENCY=1` as the documented control.
Re-running this experiment needs a quota that can complete one paper
sequentially; until then the number cannot be chosen from measurement.

**What the runs DID prove, live:**
* The **Alembic adoption path works on real data.** The restored copy was the
  legacy pre-Alembic schema (INTEGER marks, no `grading_error_code`).
  `stamp 0001` + `upgrade head` migrated it: existing marks preserved
  (`1` → `1.00`, `3` → `3.00`), and an autogenerate comparison reported no drift.
* **C1 holds live.** Six questions carried images, two carried BOTH reference and
  student images; zero leakage between the slots, checked structurally without
  reading image contents.
* **C6 holds live.** 16 graded and 23 failed in run A, and no row ever carried
  both a score and an error code. One question scored a genuine `0.0` alongside
  23 `NULL` marks with codes — the distinction the whole invariant exists for.
  Every run ended `grading_incomplete` with `graded_at` unset.
* **C7 storage holds live.** Totals persisted to `NUMERIC(7,2)` (`31.00`, `3.00`,
  `0.00`). No fractional score was produced by this marking scheme, so the
  fractional path was not exercised beyond the existing PostgreSQL verification.
* **The concurrency machinery is sound.** At concurrency 3 all 39 questions were
  accounted for, failures were isolated per question, results came back in
  question order, and there were no session, transaction or duplicate-write
  errors. The provider-neutral error taxonomy classified every failure
  (`rate_limit`, `malformed_json`, `no_student_evidence`).
* **Result consistency:** the three questions graded in both runs A and B scored
  identically.

**FINDING 4 — `create_async_engine(..., echo=True)`** logs every statement,
including the grading reason the model wrote about a student's answer. It also
raised a `UnicodeEncodeError` in the logging handler on a Windows console (a Ω
in a physics answer). Verbose SQL echo is a privacy and noise problem in a
grading system. Not fixed here; recorded below.

### Segmentation & Structure Integration v1 — DONE
A structured representation of an answer sheet, and the seam a model plugs into.

The crop workflow persisted only a cropped PNG: page identity, geometry,
reading order and part labels were discarded or burned into pixels and
recovered later by asking a model to read a number off an image. This phase
adds a real structure alongside it. **Additive — the crop workflow is
untouched and still works.**

```
Page (answer script + page_index)
  ↓  SegmentationRequest
FakeSegmentationProvider.segment_page        (backend/ai/providers/fake_segmentation.py)
  ↓  SegmentationResponse[RegionPrediction]
validate_predictions                         (backend/ai/segmentation.py)
  ↓  Region[]  — deterministic gate
DocumentRegion rows, status="proposed"       (backend/models/tables.py)
  ↓  human accept / modify / reject
status accepted|modified                     (POST/PATCH/DELETE /regions)
  ↓
future HTR / HMER / diagram / grading pipeline
```

**Proposals are not annotations.** A provider's output is stored as `proposed`
and nothing downstream may treat it as truth. Prior experiments found this
class of model silently omits content, over-merges regions, misreports which
page it was given, and self-reports confidence that does not correlate with
being right — so `validate_predictions` checks every proposal against facts the
application already holds, and drops what disagrees with a reason code.

Two rules follow directly from those failure modes:
* **page_index comes from the REQUEST, never the response.** We know what we
  sent.
* **an unclear question assignment stays unassigned.** A region naming a
  question the exam does not have is kept — the content is real — but left
  unattached rather than filed under the wrong question.
* **confidence is stored as opaque metadata and never gates acceptance.**

**Geometry is normalised to the page: every coordinate a float in [0, 1].**
`crop-edit.js` renders through `getViewport({scale})` and reads
`getBoundingClientRect()`, so pixel numbers depend on zoom, DPR and window
size; a fraction of the page is the only figure that survives re-rendering, or
re-rasterising the PDF at a different DPI. Rect (`x,y,w,h`) and polygon
(`points`) both round-trip exactly; a backwards drag is normalised, not
rejected. Bounds are derived deterministically, rounded to the same 6 dp.

**Vocabulary** (`backend/regions/schema.py`), nine values, each earning its
place: `handwritten_text`, `printed_text`, `math`, `diagram`, `table`,
`crossed_out`, `teacher_marking`, `page_furniture`, `other`. `math` is separate
because handwritten mathematics needs HMER not HTR — typing a region is a
routing decision. `crossed_out` and `teacher_marking` exist so struck-through
work and the teacher's red pen are REPRESENTED rather than silently included or
dropped; `STUDENT_ANSWER_TYPES` is what a grading pipeline filters on, and
neither is in it. `LEGACY_BUCKET_TO_REGION_TYPE` maps the three old crop
buckets on.

**Lifecycle:** `proposed → accepted | modified | rejected`; a human-drawn
region is born `accepted`. Editing a proposal promotes it to `modified`
automatically rather than trusting the client to say so — the split between
"the model was right" and "a human fixed it" is the only record of how good the
model actually was. Rejected model proposals are KEPT as rejected (the other
half of that record); a human's own region is deleted outright.

**Provenance:** `source` is `model | human`, structural only — never a vendor
name in the schema. `provider` / `model_name` / `prompt_version` /
`provider_metadata` are optional columns the domain never reads. No raw
provider response and no key is persisted.

**Database:** one new table `document_regions` (Alembic **0004**, purely
additive). `exam_id` is carried directly so authorization is one join;
`answer_script_id` / `material_id` name the source document. `question_id` is
`ON DELETE SET NULL`, not CASCADE — an unassigned region is legitimate, so
deleting a question must orphan its regions rather than destroy the student
work they point at. Multi-page answers need nothing special: regions carry
their own `page_index`, so one question owning regions on pages 3 and 4 is the
default case, not a feature.

**API** (`backend/routers/regions.py`), no provider name in any path:
`GET /answer-scripts/{id}/regions`, `POST .../segmentation`,
`POST .../regions`, `PATCH /regions/{id}`, `DELETE /regions/{id}`,
`POST .../regions/reorder`. Authorization reuses the exam policies and is
always resolved FROM the answer script or the region, so authority over one
exam grants nothing on another. A student may read regions on their own script
and may not annotate; managers do everything.

**Frontend:** one additive module, `frontend/region-overlay.js` — fetch, draw
rects and polygons over the crop editor's existing per-page `.overlay`, label
each with type/question/reading position, dash the non-answer types, fade
proposals. No editing, no redesign; `regionToGeometry` is the inverse a future
editor will need. Placement is pure percentages, which is the payoff of the
normalised coordinate decision.

**Verified on PostgreSQL 15.19** (disposable container): fresh DB → head, and a
legacy pre-Alembic DB → `stamp 0001` → head. Both reached 0004 with no drift;
all six legacy crop columns intact; FK delete rules confirmed
(`c,c,c` and `n` for `question_id`).

- 510 tests (437 + 73). **NO LIVE PROVIDER CALLS — zero quota.**

### Structured Region Evidence Integration v1 — DONE
Accepted regions now become the evidence grading actually runs on.

The region contract existed but nothing read it: `build_grading_evidence` still
took the legacy `ans_*_images` path lists. This connects the two, additively.

```
AnswerScript (file_path, resolved from the DB -- never a client path)
  ↓  load_regions_for_question      one JOIN; exam + student + question in SQL
DocumentRegion[]
  ↓  select_gradeable_regions       accepted/modified, STUDENT_ANSWER_TYPES,
  ↓                                 this question only, ordered
  ↓                                 (page_index, reading_order, id)
PageRenderer.page                   pypdfium2 for PDFs, Pillow for rasters
  ↓  crop_region                    normalised 0..1 -> pixels
CropWorkspace.write                 temp dir, deleted in a `with`
  ↓  build_region_image_set
ImageSet  →  GradingEvidence.student_images
```

Reference evidence is untouched: it still comes from `Question.ms_*_images`.
No structured-region contract exists for marking schemes and inventing one here
would be audit C1 in a new costume, so a student region cannot reach the
reference slot — asserted.

**Precedence** (`backend/grading/region_evidence.build_evidence`):
usable accepted/modified regions → structured; otherwise → legacy crops,
byte-for-byte as before. Structured **replaces** the legacy student images, it
is never added to them, so the same answer is never sent twice. **SUPERSEDED**
by Region Safety & Evidence Semantics v1 below: whole-set replacement lost
unrelated legacy evidence, and precedence is now decided per category.

**Fallback is not a catch-all.** It covers "there is nothing structured to
use". It does NOT cover "there was, and producing it failed": a render,
geometry or missing-source failure raises `RegionEvidenceError`, and the caller
records it as a preparation failure with NO mark (C6). Grading a student
against stale crops while their teacher believes the new annotations are in
force would be a quiet wrong answer.

**Excluded from evidence, deliberately:** `proposed` (a model's unreviewed
guess must not move a mark), `rejected`, `teacher_marking`, `crossed_out`,
`page_furniture`, `printed_text`, and any region with no `question_id`.

**Crops.** Rectangles are cut at the bounding box. **Polygons** are cut at the
bounding box with everything outside the polygon painted white — keeping a
plain rectangular image every consumer handles, while dropping neighbouring
content a box alone would drag in. White, not transparent, because the
downstream graders expect an opaque page-like background. Nothing is drawn onto
the pixels: no question number, part, order or type. Those stay columns.

**Type → bucket:** `handwritten_text → text`, `table → table`,
`diagram → diagram`, and `math → diagram`. **SUPERSEDED** by Region Safety &
Evidence Semantics v1 below: `math` is now its own category and handwritten
text is no longer attached at all.

**Lifecycle.** `CropWorkspace` is a context manager over a private temp
directory, removed on success, failure and cancellation alike. Nothing is ever
written to `uploads/`. `grade_exam_logic` opens one workspace around phases 1–3,
because crops built for phase 1's prompts must still exist when phase 2 uploads
them.

**Performance.** One `PageRenderer` per evidence build caches rendered pages, so
a page carrying four regions is rasterised once (asserted). No global cache.

**Security.** The region query joins `answer_scripts` and constrains exam,
student and question in SQL, so cross-student and cross-exam regions are
unreachable by construction. Source paths come from the authoritative
`AnswerScript` row, never from a client.

**Observability.** One safe line per build: `question_id`, `student_id`,
`evidence_source`, `region_count`, `page_count`, `rendered_pages`, bucket counts
— no answer text, no marking scheme, no paths.

**New dependency:** `pypdfium2==4.30.0`, one self-contained wheel with a bundled
binary and no system dependency (unlike pdf2image/poppler). Its absence is an
explicit `page_render_unavailable` preparation failure, not a crash.

- 557 tests (510 + 47). **NO LIVE PROVIDER CALLS — zero quota.**

---

### Region Safety & Evidence Semantics v1 — DONE

A report-only audit of the two region commits concluded: **no revert needed**,
the normal grading workflow is unchanged, the legacy path still works, and the
region architecture is useful AI-first infrastructure worth keeping. It found
four narrow correctness/safety defects, all confirmed against HEAD and all
fixed here. No feature was added and nothing was reverted.

**A — the production route defaulted to the fake segmentation provider.**
`SegmentationRun.provider` defaulted to `"fake"`, and the router is registered
in production, so an ordinary authenticated request against a real answer
script persisted synthetic regions on a real student's paper — indistinguishable
from a model's once stored. Fixed by removing provider choice from the request
body entirely. The provider is now resolved from deployment configuration
(`resolve_segmentation_provider`, reading `CG_AI__SEGMENTATION__PROVIDER` like
every other task), the segmentation task's configured provider is **empty by
default** (`NO_PROVIDER`), and an unconfigured or misconfigured deployment gets
`ProviderNotConfiguredError` → **503 `segmentation_not_configured`** with
nothing written: no rows, no `replace_existing` deletions, no commit. The fake
stays available to tests through that same configuration key (the
`segmentation_configured` fixture) — never through a client.

**B — mathematics was described to the grader as a diagram.** `math` mapped to
the `diagram` bucket, so the prompt told the model that an attached handwritten
derivation was a picture. `ImageSet` now has a fourth category and the mapping
is honest.

**C — structured evidence replaced the whole student `ImageSet`.** One accepted
table region deleted a valid legacy diagram from the prompt, silently. Fixed by
per-category precedence.

**D — handwritten-text crops were attached alongside the recognised text.**
`handwritten_text → text` would have sent the same sentences twice. Now
explicitly not attached.

**Evidence categories** (`backend/grading/evidence.py`), both sides:

```
text     "text images"             math   "mathematical working"
table    "tables"                  diagram "diagrams"
```

`ImageSet.descriptor()` now **enumerates every category that holds a file**, in
that order — "mathematical working, tables and diagrams" — instead of returning
the first phrase that matched and leaving the rest unannounced. A category with
no files is never mentioned.

**Structured vs legacy precedence, per category:**

```
math     structured crops if any were produced, else legacy (always empty)
table    structured crops if any were produced, else legacy ans_table_images
diagram  structured crops if any were produced, else legacy ans_diagram_images
text     never attached on the student side, from either source
```

So a structured table and a legacy diagram both survive, while a structured
diagram and a stale legacy diagram are never both sent. `EvidenceSource` gained
`mixed` for that middle case, so telemetry does not overstate how much of a
prompt reflects the current annotation.

**Handwritten-text policy — decided, not defaulted.** The recognition stage
already turns handwriting into `answer_text`, and the legacy path has always
passed `student ImageSet.text = []` for that reason. A `handwritten_text`
region is therefore selected, counted and question-assigned, but **never
rendered and never attached**: no crop is generated at all, so it costs no
rasterisation. If a later phase wants raw handwriting as a distinct multimodal
signal, it belongs in `text` *with the recognised text suppressed*, not
alongside it.

**"Usable" now means evidence actually produced**, not "an accepted region
exists" (`attachable_regions`). An accepted handwritten-text region attaches
nothing, so it cannot displace a valid legacy diagram — and a missing source
document in that case is not a preparation failure, because there was never any
structured evidence to fail at producing.

**Fail-closed behaviour preserved.** Where a region *would* attach an image and
the source document is missing or unrenderable, it is still
`RegionEvidenceError` → preparation failure with NO mark (C6). Silently
substituting a legacy crop could produce a plausible but wrong mark, and the
repository holds no evidence that a legacy crop corresponds to the current
accepted state.

**Non-answer types unchanged and re-asserted:** `crossed_out`,
`teacher_marking`, `page_furniture`, `printed_text`, `other` never become
student evidence, and `proposed`/`rejected` still never grade.

**`PROPOSED` was NOT made gradeable.** A raw model proposal and a
deterministically validated result are different things, and only the second
should grade unattended. The intended future shape is
`model prediction → deterministic validation → auto-accepted/validated →
grading`, with anomalies staying `proposed`. `DocumentRegion.status` is plain
text with no DB enum or check constraint, so a future `auto_accepted` /
`validated` value needs **no migration** — only a change to
`RegionStatus.ALL`/`USABLE` and the tests that pin them. Deferred deliberately:
real segmentation integration should decide that policy, not this phase.

- 607 tests (557 + 50). **NO LIVE PROVIDER CALLS — zero quota. No migration.**
- C1 PASS · C6 PASS · C7 PASS.

---

### Offline Pipeline Harness v1 — DONE

Every earlier phase validated one stage in isolation. This one runs the whole
automatic pipeline joined up, offline, and asserts what comes out the far end.

**The production path exercised**, with nothing replaced but the vendor call:

```
tasks._process_and_grade                          the Celery task body
  -> geminiAPI.process_answer_text_images_logic    recognition (batched)
       -> ai_services.recognise_answer_images
            -> ai.services.run_task -> run_with_retries -> PROVIDER
  -> geminiAPI.grade_exam_logic                    grading, three phases
       1 LOAD     build_region_aware_evidence -> _build_diagram_prompt_parts
       2 GRADE    run_bounded(_grade_one_question_compute)
                    -> grade_answer_with_parts -> PROVIDER
                    -> grading.result.parse_grading_response
       3 PERSIST  _persist_and_report / _record_grading_failure
  -> examStats.add_exam_result_internal            aggregation
       -> grading.aggregation.aggregate_student_result
  -> exams.set_exam_stage                          workflow state
```

The stub is installed at the `TextTaskProvider` seam (conftest's
`RecordingProvider` place), so prompt assembly, part ordering, evidence
composition, batching, retry, telemetry, validation, persistence, aggregation
and the stage transition are all real. `AsyncSessionLocal` is pointed at the
test engine; the Celery BROKER is the only uncovered link — the task body is
invoked directly, exactly as `process_and_grade_exam` invokes it.

**The synthetic exam** (`backend/tests/test_offline_pipeline.py`), six
questions, no real student data:

```
CG-Q1  5   text-only, no regions               legacy path       -> 4.0
CG-Q2  3   marking-scheme AND student diagram  the C1 pair       -> 2.5
CG-Q3  2   accepted math + handwritten region
           + a legacy table                    structured/mixed  -> 1.25
CG-Q4  2   a wrong answer                      a REAL zero       -> 0.0
CG-Q5  4   failure injection                                     -> NULL
CG-Q6  3   no response row at all              nothing submitted
```

**Complete success:** all five marks persist, `answer_text` lands from
recognition, no failure codes, `ExamResult` = 11.25 / `graded` / `graded_at`
set, exam stage 7. CG-Q6 does not block finalisation — an absent response row
means nothing was submitted, which is not a grading failure (the documented
`aggregate_student_result` rule).

**Partial failure:** CG-Q5 alone has `marks_obtained = NULL` and
`grading_error_code = invalid_request`; its four siblings keep their marks and
carry no code; `ExamResult` = 7.75 / `grading_incomplete` / `graded_at` NULL;
exam stage 6. Per-question isolation is real: a sibling's failure rolls nothing
back.

**Real zero vs failure:** CG-Q4 keeps `0.0` with a reason and no code while
CG-Q5 stays NULL with a code, and the 7.75 total proves neither was mistaken
for the other.

**Structured vs legacy:** CG-Q1 attaches nothing (text only). CG-Q3 sends the
structured maths crop AND the legacy table — an accepted maths region does not
erase an unrelated legacy category — while its accepted handwritten-text region
attaches nothing, because the recognised `answer_text` is already in the
prompt. The prompt says "mathematical working" and never "diagram". Crops are
asserted to still exist at the moment of the provider call.

**Fail-closed:** with the answer script missing, CG-Q3 fails `source_missing`
with no mark and no provider call, while the region-free siblings still grade.

**Provider calls:** one recognition batch (5 images, batch size 5) and exactly
one grading call per question with evidence — 5, never 6, never a repeat. A
retryable error is retried exactly to the budget (1 + 2); a non-retryable one
is not retried at all.

**Re-execution is safe and self-healing.** A second complete run creates no
duplicate `ExamResult` or `QuestionResponse` rows and does not double the
total. Re-running after a failure clears the stale `grading_error_code`, writes
the mark, finalises the result and advances the stage — the documented recovery
path, now proven rather than assumed.

**Logging:** across a whole run, CogniGrade's own loggers carry ids, counts and
provider-neutral categories, never the recognised answers, the marking scheme,
provider output or evidence paths. (The SQLite driver's DEBUG statement log is
out of scope: every driver echoes bound parameters at DEBUG.)

**BUG FOUND AND FIXED — the exam stage ignored the outcome.**
`_process_and_grade` ended in an unconditional `set_exam_stage(exam_id, 7, db)`
— stage 7 being "Graded" — which ran even when aggregation had just written
`grading_incomplete` with no `graded_at`. Two persisted records of the same
fact then disagreed. Fixed narrowly: `exam_result_is_final` reads the status
the aggregation actually wrote, and the task sets `EXAM_STAGE_GRADED` or
`EXAM_STAGE_GRADING` accordingly — named constants, so no bare `7` remains.
This REVERSES the earlier "correct as it stands" note, whose premise was wrong:
the vocabulary on `Exam.exam_stage` gives 6 = "Grading", 7 = "Graded". Severity
MEDIUM, not high: `exam_stage` gates nothing in the backend, and result release
is still gated by `ExamResult.status`.

**Still open, deliberately not fixed:** `exam_stage` is EXAM-wide while
`_process_and_grade` is PER-student, so across many students the stage reflects
whoever ran last. That was true before this fix and remains true after it; a
correct exam-wide signal needs an all-students completion query and is a
product decision, not a bug fix.

**Not covered:** the Celery broker itself, and PostgreSQL — the harness runs on
the suite's in-memory SQLite because no Docker daemon was available in this
environment. SQLite's dynamic typing means this proves the ORCHESTRATION
carries 2.5 / 1.25 / 11.25 without truncation, not that the column type is
NUMERIC; that half stays covered by the static assertions and the earlier
PostgreSQL runtime verification.

- 621 tests (607 + 14). **NO LIVE PROVIDER CALLS — zero quota. No migration.**
- C1 PASS · C6 PASS · C7 PASS, now end to end rather than per unit.

---

### MVP Deployment & Live Demo v1 — DONE

**COGNIGRADE GRADED A REAL EXAM, LIVE, END TO END.** Five questions, real
Gemini, real orchestration, six provider requests, one clean pass.

```
Q1  5 marks  text answer                 -> 5.0
Q2  4 marks  half answered               -> 3.5     fractional
Q3  3 marks  marking-scheme + student diagram -> 2.5   fractional, C1 pair
Q4  2 marks  wrong answer                -> 0.0     a REAL zero
Q5  2 marks  short correct answer        -> 2.0
                                   ExamResult 13.0 / graded / graded_at set
                                   exam stage 7 (Graded)
                                   no grading_error_code anywhere
```

Run through `tasks._process_and_grade` — the exact coroutine the Celery task
runs — against a disposable SQLite file with synthetic rendered answer sheets.
No real student work, no production database, `gemini-3.6-flash`, grading
concurrency 1, one retry.

**Three configuration blockers, all fixed before any quota was spent:**

1. **The credential was never read.** `.env` holds `GEMINI_API_KEY`; the
   adapter read only `GEMINI_API_KEY_1..n`, so a deployment with a valid key
   configured nothing. `GEMINI_API_KEY` is now canonical, the numbered form is
   an explicit legacy fallback, values are stripped (a trailing space in a
   `.env` line reads as a bad key), and the missing-credential error names the
   VARIABLE, never a value.
2. **SQL echo was hard-coded on.** `create_async_engine(..., echo=True)` logged
   every statement with its bound parameters — recognised answers, grading
   reasons, marking-scheme text. Now `DATABASE_ECHO`, default **false**.
3. **The Celery entry point could not start.** `get_event_loop()` has raised
   `RuntimeError` since Python 3.12, so grading would not launch on anything
   newer than the container's 3.11. Now `asyncio.run`, with the engine disposed
   per task so pooled connections never outlive their loop.

**Then the live blocker, found by running it:**

4. **The File API rejects the key that generation accepts.** With everything
   configured, every call carrying an image still failed `authentication` while
   text-only grading worked. `genai.upload_file` in google-generativeai 0.8.4
   goes through the REST discovery client, which authenticates by appending
   `?key=` and rejects the newer `AQ.`-format keys outright:

   ```
   HttpError 400 ... "API key not valid. Please pass a valid API key."
   reason: API_KEY_INVALID
   ```

   while gRPC `generate_content` accepts the same key and answers normally.
   Since every recognition call and every diagram question carries an image,
   this failed the entire product on a working key. **Media is now sent inline**
   (`{"mime_type", "data"}`) over the path that works. That also retires the
   upload/delete lifecycle: nothing is created provider-side, so nothing can
   leak. Ceiling: `MAX_INLINE_REQUEST_BYTES` (15MB/request), which page crops
   are nowhere near; oversize media gets an explicit provider-neutral refusal.

**A false zero in the student UI — fixed.** `scriptPage.htm` rendered
`${row.marks_obtained || '0'}/${row.max_marks}`. `null` is falsy, so a question
that FAILED grading showed the student `0/5` — identical to an earned zero.
That is the one distinction the whole backend refuses to blur, erased in the
last line that touches it. Now compared against null explicitly: a real 0 still
shows as `0/2`, an ungraded question shows "Not graded". Verified against the
rendering logic for earned-zero, fractional, full, null and absent.

**Quota discipline.** Budget computed before calling: 1 recognition batch + 5
grading = 6 requests, worst case 12 with one retry each, against a free-tier
cap of 20/day/model/project. The runner carries a HARD LOCAL CAP that refuses
call 13 before it reaches the network. A dry run against a stub proved the
fixture first, for free. Total live requests consumed on the successful day:
**6 for the run**, plus 4 spent diagnosing (2 in the failed pre-fix attempt, 2
isolating upload vs inline).

**Live status:** C1 PASS (marking scheme in the reference slot, student diagram
in the student slot, different files, reference first — checked on the parts
actually sent). C6 PASS (the pre-fix failed run produced NULL marks, safe
codes, `grading_incomplete`, `graded_at` NULL and stage 6 — never a zero, never
a false final). C7 PASS (3.5 and 2.5 persisted and aggregated to 13.0).

- 647 tests (621 + 26). **No migration.**

---

### UI-1 — Stop the UI lying about marks — DONE

First task of the UI/demo phase, after a read-only frontend audit. Two
correctness defects, both on surfaces that report a student's marks. No styling,
no workflow change, no new architecture.

**A — the course card invented every number it showed.** `displayExams` in
`frontend/courses.htm` derived the student's result from the maximum:
`points_possible * 3/4` as "Your Score", `* 7/8` as "Highest", `/ 2` as
"Average", with an unconditional "Graded" badge. Every student saw a fabricated
75% on every exam — including a student whose grading had FAILED, which is the
one distinction C6 exists to protect, erased on the first screen of the product.
The professor card was the same illness in literals: `Uploaded: 45`,
`Checked: 42`, `Average Marks: 25`, and a completion tick on every exam.

The card now READS instead of deriving. `GET /classes/{id}/classwork` already
carried `user_result` = `{score, status, is_final, ...}` per exam, and already
withholds `score` unless `is_final` — so a partial total could never have
reached the page to be mistaken for a final one; nothing consumed the field.
Three states: a final score (an earned `0` prints as `0`, `2.5` stays `2.5`),
`Partial`/"Not final" for `grading_incomplete`, `Not graded` otherwise. Highest
and average are not in that payload and were therefore **removed rather than
approximated** — the per-exam Results page is where real cohort numbers live.
Same for the professor card: only `points_possible`, which is a real field.

**B — the C6 student-facing fix was defeated by its own endpoint.** MVP
Deployment v1 changed `scriptPage.htm` to compare `marks_obtained` against
`null` explicitly. But `studentBackend.get_exam_evaluation` coerced a missing
mark to the empty STRING, which passes a `!== null && !== undefined` guard — so
an ungraded question rendered as `/5` under the label "Marks", still claiming a
grade that does not exist. One line: `else ""` → `else None`, matching the
contract the manager-facing `/exam/{id}/student-evaluation/{sid}` has always
used. The numeric field now carries a number or nothing.

**Semantics verified, both surfaces:** `0.0` is an earned zero and shows as `0`;
`0.5 / 1.5 / 2.25 / 3.75` survive unrounded; a `NULL` mark serialises to JSON
`null` and shows "Not graded"; a failed question stays distinguishable from a
zero; an incomplete run never renders as graded.

- 655 tests (647 + 8). Three of the eight were verified to FAIL against the
  pre-fix endpoint. The frontend logic was driven directly with a DOM stub over
  seven cases (earned zero, three fractions, full marks, no result row, pending,
  `grading_incomplete`). **No migration, no live provider calls, zero quota.**
- C6 PASS · C7 PASS, now at the last line that touches a mark.

---

### UI-2 — Fix the broken document viewers — DONE

Security Foundation v1 removed the public `/uploads` StaticFiles mount, but only
`crop-edit.js` was migrated to `/protected-files/...`. Nine other live call
sites still built `"/api/" + file_path` by hand, so every one of them had been a
404 since that commit. **The public mount was NOT restored** — this phase moved
the callers onto the authorized route.

**What was actually broken.** Two viewers rendered blank: the student's document
pane in `scriptPage.htm` and the extracted-text modal in `exam.htm`, plus the
drop-zone thumbnail. Worse, four *re-upload* paths (`saveAllFiles`,
`extractTextForSection`, `openModifyModal`'s per-file extract,
`extractQuestionLabels`) fetch a stored file back and re-post it — and caught the
404 with a bare `console.error`, dropping the file from the `FormData`. The
request still went out, just without that file, so re-processing an exam whose
files came from the database asked the model to extract from nothing.

**Files are addressed by row, never by path.** Uploaded documents now go through
`authFetch` → `response.ok` → `blob()` → object URL. A filesystem path never
appears in a URL and never has to be trusted coming back in.

**Two routes added to `backend/auth/files.py`** — `GET /protected-files/material/
{id}` and `GET /protected-files/answer-script/{id}`. The existing doc_type route
answers "the marking scheme for exam 5" and returns the FIRST match, but
`POST /exam/save-files` deduplicates by (title, exam, type), so an exam can
legitimately hold several question-paper files and the multi-file upload UI
needs to address the second one. The id form applies **exactly** the same
capability table — question paper to participants, marking scheme and solution
script to managers only, an answer script to its owner or a manager (enrolment
required, matching the doc_type branch rather than the looser `ctx.owns`) — so
holding an id grants nothing that holding the exam id would not. Both resolve
the exam FROM the row, and both pass through `_resolve_within_root`.

**Failure is stated, not blank.** A document that cannot be fetched shows
"This document could not be loaded." / "Preview unavailable." Nothing renders a
status code, a stack trace, a provider error or a path. One object URL is alive
per viewer, revoked when paging; a load token discards a slow fetch whose page
the user has already left.

**Not fabricated:** `scriptPage.htm`'s two "View ... Image" buttons rendered
`/api/placeholder/600/400` — a URL that never existed — under the heading "Your
Answer Image". The buttons and their stub are removed rather than pointed at
something plausible. Real per-question crops ARE servable
(`/protected-files/response/{id}/{kind}/{index}`), but the student evaluation
endpoint returns neither the response id nor the kind/index split, so wiring
them up is a feature and not this phase.

**Profile pictures untouched:** they resolve to `./profile_pictures/...` and
`/profile_pictures` is still mounted, so those four call sites are correct as
they stand and were not modified.

- 670 tests (655 + 15). The 15 cover the new routes' capability table,
  anonymous access, cross-exam authority, missing ids, non-exam material types
  and the upload-root containment guard. Frontend behaviour was driven from the
  files' own source in a VM harness over 34 checks (addressing, authenticated
  request, success, 403/404/500/network failure, object-URL revocation, stale
  in-flight load). **No migration, no live provider calls, zero quota.**

---

### UI-3 — The instructor starts AI grading — DONE

The product described itself as AI-first while the only thing that could start
the AI was a student finishing a cropping session. `crop-edit.js` was the sole
caller of `enqueue-processing` in the whole frontend, and the teacher's final
setup panel read *"Automatic Grading will begin once students annotate their
answer scripts."*

**How grading is triggered now.** An instructor opens the exam at stage 6 and
presses **Start AI grading** against a named student. Submitting a prepared
script no longer enqueues anything.

**Backend change: yes, and it was necessary.** `POST /exam/{id}/enqueue-
processing` read `current_user.id` and nothing else, so a manager calling it
queued a run against their OWN id — no answer script, no responses. The route
now takes an optional `student_id`, honoured only for exam managers, matching
the pattern `/protected-files/exam/{id}/document/{type}` already uses. A student
may still run their own paper and may not name anyone else. One new policy
helper, `assert_student_enrolled_in_exam`, checks the TARGET the way
`require_question_in_exam` checks a question belongs to its exam:
`assert_self_or_exam_manager` says whether the caller may act, not whether the
id they named is a student of this exam.

**A readiness guard, and it is not tidiness.** `aggregate_student_result`
finalises when every response that EXISTS carries a mark — vacuously true of
ZERO responses. A run for a student whose script was never prepared would
therefore aggregate to `0.0`, stamp `graded` and set `graded_at`: a fabricated
final zero, exactly what C6 exists to prevent. The old flow could not reach that
state because the only trigger created the rows; moving the trigger makes it
reachable, so an unprepared script is refused with **409** and never enters the
pipeline. Four tests fail against the pre-fix route on this alone.

**`submission_status` gained `prepared`.** `pending` alone could not distinguish
a script nobody has submitted from one submitted and waiting to be graded — days
apart now that a human starts the run. One count query, no schema change.

**How preparation is framed.** The crop editor is untouched and still produces
the evidence grading runs on. What changed is that a student is no longer
*dropped into* it: the page offers one action, "Submit my answer script", with a
sentence saying the AI reads and evaluates the answers afterwards. Once prepared,
the student sees "Answer Script Submitted / awaiting grading" and is given
nothing to do. Preparation is a step before the product, not the product.

**Known limitation, deliberately not hidden.** Production segmentation still
does not exist (`CG_AI__SEGMENTATION__PROVIDER` is empty, the only adapter is the
test fake), so somebody must still mark where each answer is, and that somebody
is still the student — `POST /exam/{id}/question_response/{type}` writes
`student_id = current_user.id`, so an instructor cannot prepare a script on a
student's behalf without a further identity change. The UI does not claim
otherwise. What is automatic today: recognition, question mapping, grading,
aggregation and result generation, all from one instructor action.

- 691 tests (670 + 21). Twelve of the 21 fail against the pre-fix route,
  including a student naming another student getting a silent 200. Frontend
  driven from page source in a VM harness over 32 checks (endpoint and student
  id, success state, 409/403/404/500 copy, duplicate-click guard, roster
  fallbacks, and copy assertions that the annotation framing is gone).
  **No migration, no live provider calls, zero quota.**
- C6 PASS · C7 PASS. UI-1 and UI-2 semantics unchanged.

---

### Google sign-in fix — DONE

Found during live UI inspection: the Google button returned
`401 invalid_client — The OAuth client was not found`. **Two independent
faults**, either of which alone breaks the flow.

**1. Credentials absent.** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` were
unset, and `oauth.register()` passes them through with no validation, so an
EMPTY client id went to Google. The app started, rendered the button, and said
nothing until a user clicked it. (Both variables are also blank in the older
`DEP25-G06-CogniGrade` checkout — Google sign-in has never been configured in
this project.)

**2. The callback was derived from a rewritten path.** `login_google` used
`request.url_for("auth_via_google")`. nginx rewrites `^/api/(.*)$ -> /$1`
before proxying, so the backend never sees the `/api` prefix the browser used
and generated `http://localhost/auth/google` — which nginx routes to the
FRONTEND container and 404s. Confirmed from the live `Location` header. This
fault would have survived adding credentials.

**The fix: state the callback, do not infer it.** New `GOOGLE_REDIRECT_URI`.
Google requires the redirect URI to match a registered value exactly, so it is
deployment configuration by nature; stating it also survives TLS termination,
where a derived URL would claim `http://`. Rejected: routing `/auth/google` in
nginx (puts OAuth knowledge in the proxy, still wrong under TLS) and
`root_path="/api"` (changes every `url_for` in the app).

**Enabled only when all three are set.** Partial configuration is the bug, so
it is now loud: a startup WARNING naming the unset variables, and **503** from
both OAuth routes instead of a redirect Google rejects. `config.py` gained
`missing_google_oauth_settings()` / `google_oauth_enabled()` /
`google_redirect_uri()`, all reading the environment at call time so the
enabled check and the callback cannot disagree. Names only ever reach the log;
the browser gets one safe sentence.

**Not fatal, deliberately.** Google sign-in is optional and email/password
login does not depend on it, so a deployment setting none of the three is
normal and gets an INFO line.

```
GOOGLE_CLIENT_ID       from Google Cloud Console (OAuth 2.0 Client ID, Web application)
GOOGLE_CLIENT_SECRET   same client
GOOGLE_REDIRECT_URI    docker compose  http://localhost/api/auth/google
                       backend on 8000 http://localhost:8000/auth/google
                       behind TLS      https://<domain>/api/auth/google
```

The same URL must be registered as an Authorised redirect URI on the OAuth
client, or Google answers `redirect_uri_mismatch`.

- 712 tests (691 + 21). Seven fail against the pre-fix routes. Verified live in
  the Docker stack: `/api/login/google` and `/api/auth/google` gave a safe
  refusal and no Google redirect; `POST /api/login` still reached its own
  validation. **No secret committed; `.env.template` carries names only.**
  (The refusal was a 503 JSON body at this point. Replaced in the next
  section — see *Demo-safe authentication entry path*.)

---

### Demo-safe authentication entry path — DONE

Live testing with the credentials configured surfaced three more failures. None
of them is a wrong grade or a wrong permission; all three are the first thing a
demo audience sees.

**1. `502 Bad Gateway` after every `docker compose ... up -d --build`.**

nginx resolves a host name written LITERALLY in `proxy_pass` exactly once, when
the configuration is loaded, and reuses that address for the life of the worker.
`up -d --build` recreates `portal_backend`, which can come back on a different
address on the compose network, and nginx went on dialling the old one. Only
`docker restart portal_nginx` cleared it.

Reproduced deterministically rather than guessed: stop the backend, hold its
address with a throwaway container on `dep25-g06-cognigrade_internal`, start the
backend so it has to move. nginx then logged

```
connect() failed (111: Connection refused) while connecting to upstream,
upstream: "http://172.19.0.5:8000/check-session"
```

while the same backend answered `401` normally on its new address from inside
the nginx container. A plain `up -d --build` reproduced it too: the backend
moved `.5 -> .6`, nginx (untouched) kept dialling `.8`, and `/api/` was 502.

**Fix: look the upstream up per request.** `resolver 127.0.0.11 valid=10s
ipv6=off;` plus `set $backend_upstream http://backend:8000;` /
`proxy_pass $backend_upstream;`, and the same for the frontend. nginx only
defers resolution when the upstream comes from a VARIABLE, and a variable needs
an explicit resolver — `127.0.0.11` is Docker's embedded DNS. The `/api` rewrite,
the headers, and the ports are untouched: nothing new is published to the host.
Rejected: an `upstream {}` block (resolved once at load, same bug) and adding
the backend to the host network (undoes internal-only exposure).

Verified: backend forced onto a new address with nginx never restarted, `/api/`
answered 401 within the first 5s poll and stayed up. **One-time cost:** the
config is a bind mount, so `up -d --build` alone does not reload it — apply it
once with `docker compose -p dep25-g06-cognigrade up -d --force-recreate nginx`.
After that, ordinary rebuilds need no manual nginx restart.

**2. `MismatchingStateError` rendered as raw JSON.**

In a second Chrome profile Google returned to the correct callback and Authlib
raised `mismatching_state: CSRF Warning! State not equal in request and
response.` The route caught `OAuthError` and returned
`{"success":false,"error":"Google OAuth failed"}` — a JSON body, in a top-level
browser navigation, with no way back.

**The CSRF check is untouched.** It is doing its job; a stale tab, a replayed
callback URL, a dropped session cookie or a second sign-in started over the
first all reach it legitimately. Only the presentation changed: both OAuth
routes now `303` to `/login.htm?auth_error=<code>`.

A **code**, never text. Nothing Authlib, Google or an exception produced is
copied into the URL, so a failure cannot put a state value, an authorization
code, a token or an email address into the address bar, the history or a
referrer. `login.htm` holds the three sentences (`google_session_expired`,
`google_unavailable`, `google_failed`), writes them with `textContent`, falls
back to the generic one for anything unrecognised, and clears the parameter with
`history.replaceState` so a reload does not show a stale failure.

`_clear_google_oauth_state()` drops the session's leftover `_state_google_*`
entries on failure. Authlib removes them only on the success path; left behind
they accumulate in the session cookie until it is too big to send, and then
every later attempt fails identically — a dead end the user cannot escape. The
keys are removed, never read and never logged.

**3. The unconfigured route was still a raw JSON 503 page.** Same treatment,
same reason: it is a browser navigation. Both routes now return the browser to
the login page with `google_unavailable`; the names of the unset variables still
go to the server log at ERROR, and the browser gets no status code, no
configuration name and no reason. No feature-flag plumbing and no conditional
rendering of the button — a server-side redirect was enough.

**Found while verifying, and fixed: the access log carried the credential.**
uvicorn writes the request line verbatim, so

```
INFO: "GET /auth/google?state=...&code=4-0Af..." 303 See Other
```

put a one-use authorization code and the flow's CSRF state in
`docker logs portal_backend` on EVERY sign-in, successful or not, whatever the
route itself chose to log. `backend/main.py` now installs
`RedactSensitiveQuery` on `uvicorn.access` and on root's handlers: a query
carrying `code`, `state`, `id_token`, `access_token`, `refresh_token` or `token`
is logged as `?<redacted>`. Route, method and status survive; only the values
go. Applied by argument shape, not by position, so it does not depend on how a
given uvicorn version orders its access-line arguments.

Also fixed: `test_partial_configuration_is_also_refused` reloaded
`backend.config`, which re-runs `load_dotenv()` and put a developer's real
`.env` back over the variable the test had just removed — it silently asserted
the opposite of its name on any machine with a populated `.env`. The reload was
never needed; the helpers read `os.environ` at call time for exactly this
reason.

```
nginx/nginx.conf         resolver + variable upstreams
backend/routers/auth.py  redirect-with-code, state cleanup, safe logging
backend/main.py          access-log query redaction
frontend/login.htm       the three sentences, rendered as text
```

- 733 tests (712 + 21). Live: `up -d --build` left the site and API reachable
  with no manual nginx restart; a stale callback landed on `/login.htm` showing
  “Google sign-in session expired. Please try again.” with the Google button
  ready for a clean retry; `?auth_error=<img onerror=...>` rendered as the
  generic sentence with zero injected nodes; an unconfigured backend redirected
  both routes to `google_unavailable`; email/password login unaffected; and the
  planted state and code values appeared **zero** times in the backend log.

---

### Visible-document ingestion & question replacement — DONE

A five-question test paper (31, 32, 36, 37, 39) produced **seven** questions on
`exam.htm`: 33 and 38 appeared as well. Two independent faults, found together.
Only the first caused the incident.

**1. The AI was reading a document nobody could see.**

The paper had been cut down from a full one, and the cut questions were made
invisible rather than deleted. Measured on the actual file:

```
MediaBox = CropBox = TrimBox = (0,0,595,842) on all 4 pages   -- there is no crop
"33." character box  L=28.2 R=35.1 B=187.5 T=197.5            -- INSIDE the page
rendered band 184-200pt   min=255  mean=255.0  non-white px=0  ("32." band: 7857)
native text layer         [31, 32, 33, 36, 37, 38, 39]        -- full text of both
```

Ingestion sent the uploaded FILE. `extract_question_labels` passed
`document_path` into `ProviderRequest.simple(file_paths=...)`, and
`GeminiProvider._inline` reads raw bytes with `mimetypes.guess_type()` -- so the
provider received `mime_type application/pdf`, all 489563 bytes, and read the
document's text layer. There was no rasterisation anywhere in ingestion;
`pypdfium2` was used only by `backend/regions/cropping.py` in the grading path.

The database set matched the TEXT LAYER exactly, not the rendered pages. That is
the whole finding: not a crop bug, not a hallucination, not stale rows.

**Fix: the task is given the pages, not the file.** New
`backend/ai/documents.py` renders a document to one image per visible page --
same library and same scale (2.0) as the region path, deliberately not a second
constant -- and `services.py` wraps both document-reading tasks in it. Rendering
collapses everything a page does not show (invisible text, white-on-white,
covered objects, content outside a crop) into the one representation the
uploader approved. Applied to the marking scheme too: hidden text there would
reach `ideal_marking_scheme` and be graded against.

Deliberately NOT a native text-extraction-and-filter step: reading the PDF's own
text and deciding what is visible would be a second document-understanding
engine, in the layer that exists so there is only one. **Provider-neutral by
construction** -- it hands back local file paths, which is what `FilePart` has
always carried. No route, schema, prompt, persisted format or provider contract
changed. A document that cannot be rendered raises a NAMED error; there is no
fallback to sending the file, because that fallback would silently restore the
bug on the paper least likely to be checked by hand. Cost: ~193 KB/page, so a
40-page paper is ~7.6 MB against the existing 15 MB inline ceiling, and
`MAX_RENDERED_PAGES = 60` refuses anything longer by name rather than
truncating.

**2. Reprocessing appended. It never replaced.**

Not the cause here -- all seven rows were ids 60-66, written 45 ms apart by one
run, against an exam whose questions had never existed before -- but it would
have been the cause of the next one. `/extract-question-labels` did `db.add` +
`commit` + `refresh` per row with no delete, no upsert, and there is no unique
constraint on `(exam_id, question_number)` in the live schema. The only removal
lived in the browser, behind a filename-and-size comparison, in a separate
request: a re-upload matching on name and size skipped extraction entirely, and
two files in one upload appended to each other.

Now a transactional replace. Every uploaded file is parsed first, into one
accumulated structure; then, in ONE transaction, the exam's questions are
deleted and the new set inserted. A failed extraction writes nothing, so the old
structure and the new one are never both absent. Idempotent: the same paper
twice gives the same rows. The parse moved to `_parse_question_labels`, so it
can be tested without a provider, a database or an upload.

**Refused, not destructive, once a student has responses.**
`question_responses.question_id` cascades on delete, so rebuilding the structure
of an exam that already has responses would take the responses, their recognised
text and their marks with it. That is not a re-upload's decision: the route
answers **409** and changes nothing.

**Also fixed:** `DELETE /exams/{exam_id}/questions` took
`get_current_user_required` alone, so ANY signed-in account could erase any
exam's question structure -- and cascade the students' answers and marks away
with it. Now `require_exam_manager`, like every other manager-only exam route.

**Frontend.** The client-side delete is gone: the server replaces in one
transaction, so deleting first from the browser only opened a window where a
failed extraction left the exam with no questions. The "files unchanged, skip"
heuristic is a quota guard, not a correctness rule, and as a silent skip it made
a bad extraction unfixable without renaming the paper -- it now offers a
re-process instead of refusing one. A 409 arrives as `detail` and is shown;
it used to read "Error: undefined".

```
backend/ai/documents.py       NEW  render a document to its visible pages
backend/ai/services.py             both document tasks read pages, not the file
backend/routers/geminiAPI.py       transactional replace, parser extracted
backend/routers/exams.py           the destructive delete is manager-only
frontend/exam.htm                  no client delete, re-process offered, 409 shown
```

- 762 tests (740 + 22). The fixture is a PDF built byte by byte with a real xref
  and one line drawn in `3 Tr` (invisible) mode, asserted to reproduce the real
  paper before anything else is claimed: CropBox equal to MediaBox, the hidden
  text in the layer, zero ink in its band. Then both document tasks receiving
  `.png` with PNG magic and never the PDF; page images deleted even when the
  call raises; `31,32,33 -> 31,32` leaving `31,32`; stale subparts and marks
  replaced; idempotence; 409 with the mark surviving; a failed run writing
  nothing; manager-only; an AST guard that only two `Question(...)` constructors
  exist. **No network, no key, no quota.**
- Live on exam 4: `before 31 32 33 36 37 38 39` -> `after 31 32 36 37 39`, rows
  67-71 written in one batch with 60-66 gone, the UI showing neither 33 nor 38,
  the log line reading `document normalised to 4 visible page image(s)` -- a
  count, no path and no content -- and no temporary page directory left behind.

**Watch:** question 36's label depth differed between the two runs
(`36.a.ii.I` / `36.a.ii.II` appeared in the first and not the second). That is
model variance in how deep it reads the hierarchy, not the fix, but marks are
attached at the top level and parts drive the crop workflow, so a paper's
subparts are worth one look before grading.

---

### Live database adoption: pre-Alembic -> 0004 — DONE

The reused historical PostgreSQL volume (`dep25-g06-cognigrade_postgres_data`,
created 2026-08-24) was never under Alembic control, and the newer code hit it:
`asyncpg.exceptions.UndefinedColumnError: column
question_responses.grading_error_code does not exist`, which made
`GET /exams/4/stats` a 500 and left answer-script processing unable to reach
grading readiness. Not a Celery problem; a schema-version problem.

**The baseline was measured, not assumed.** Revision 0001 was built in a
disposable `postgres:15` container and its catalog diffed against the live one
with identical queries: **188 column/index lines, 59 constraints, 16 tables and
5 enum definitions -- zero difference on every one.** That is what made
`stamp 0001` truthful rather than a guess, and it is the check to repeat before
adopting any other legacy deployment.

**Backup first.** `pg_dump -Fc --no-owner --no-privileges` run inside the
postgres container (so the password never reached a host command line), pulled
out with `docker cp`, stored OUTSIDE the repository and outside any Git tree.
Verified three ways: `PGDMP` magic and an exact byte-size match against the
in-container original, `pg_restore --list` showing all 16 tables with data
sections, and a real restore into a disposable database whose row counts,
column types and exam-4 structure diffed to zero against live. `.gitignore`
does not cover `*.dump`; the backup lives outside the repo, which is what keeps
it uncommittable.

**Adoption, with writers stopped.** `backend` and `celery_worker` are the only
services holding database credentials (`frontend` is a static build, `nginx` is
`nginx:alpine`); both were stopped, `pg_stat_activity` confirmed zero remaining
connections, then:

```
alembic -c backend/alembic.ini stamp 0001     # recorded 0001, applied NO DDL
alembic -c backend/alembic.ini upgrade head   # 0002 -> 0003 -> 0004
```

run through `docker compose run --rm --no-deps backend`, so the deployment's own
configuration resolved the URL. The stamp was verified to have changed nothing
in the schema before the upgrade was allowed to run.

**Result: `alembic_version = 0004`, `current` and `heads` agree, and
`alembic check` reports "No new upgrade operations detected"** -- head is the
current ORM schema with no residual drift. 18 tables (17 application +
`alembic_version`); the one new application table is `document_regions`.

- All six mark columns are now `numeric(7,2)`: `assignments.points_possible`,
  `exam_results.marks_obtained`, `exams.points_possible`,
  `question_responses.marks_obtained`, `questions.max_marks` (still NOT NULL),
  `submissions.grade`.
- `question_responses.grading_error_code` exists: `text`, nullable, no default.
- `document_regions` exists: 20 columns, 0 rows, three indexes, and all four
  foreign keys with the intended rules -- `question_id ON DELETE SET NULL`,
  the rest CASCADE.

**Data preserved exactly, and proved rather than asserted.** Every aggregate
count is unchanged (users 2, classrooms 4, enrollments 4, exams 4, questions 57,
question_responses 39, answer_scripts 3, materials 11, exam_results 0). Mark
sums went `176 -> 176.00`, `6 -> 6.00`, `299 -> 299.00`. An md5 fingerprint over
every question row and every response row -- ids, foreign keys and marks, with
the mark cast back through `::integer::text` -- is **byte-identical before and
after** (`c30b9d9d...`, `f1ff1058...`), so no individual value moved. Zero
non-integral values exist, consistent with a widening that cannot round.

**Exam 4 untouched by the migration:** still exactly 31, 32, 36, 37, 39 with
marks 3/3/5/4/4 (now `3.00` etc.), 1 answer script, 1 enrolled student, and
still **0 question_responses and 0 exam_results** -- the migration synthesised
no grading data.

**After restart** the backend logs `Database is under Alembic control; skipping
create_all` instead of the pre-Alembic warning, with no startup exception, and
Celery reports ready. `GET /exams/4/stats` returns **200** where it returned 500,
carrying no driver text, and `questions/parts` returns `max_marks` as `3.0`
through the `Marks` type. No provider call was made at any point.

**The schema blocker is resolved.** What has NOT been retried yet is the answer
script itself.

---

### Controlled exam-4 retry after the DB fix — PRODUCT-FLOW BLOCKER

One run of the real product action ("Start AI grading" on `exam.htm`), exam 4,
one enrolled student, one uploaded answer script.

**Result: `409` before any provider call. Zero Gemini requests, zero Celery
tasks, zero database rows changed** (pre/post snapshots diffed identical).

```
POST /api/exam/4/enqueue-processing?student_id=1
  -> 409 {"detail":"This answer script has no prepared responses to grade yet."}
UI shows: "This answer script is not ready for grading yet."
```

**The schema blocker really is gone** -- this 409 is `enqueue_processing`'s own
readiness gate in `backend/routers/routingTasks.py`, not a driver error. That
gate is correct and must stay: with zero response rows
`aggregate_student_result` would finalise vacuously and stamp a fabricated
`0.0`, which is the C6 failure it exists to prevent.

**`Error: undefined` did NOT reappear.** `startAiGrading` maps 409/403/404 to
its own copy and renders nothing from the response body. Not reproduced, so NOT
proven fixed -- it was a symptom of the earlier 500, which no longer happens.

**The real blocker: nothing automatic creates `question_responses`.** The gate
needs at least one, and every live creator is human-driven:

```
studentEdit.py:118   POST /exam/{id}/question_response/{doc_type}   the CROP EDITOR
studentBackend.py:137/277, exams.py:602, examStats.py:336           manual/teacher/query
```

`student-edit.htm`'s "Submit my answer script" only reveals `#container` and
hands over to `crop-edit.js` with "Submit All Responses" -- i.e. the student is
still asked to cut regions before the AI may run. So the AI-first path stops one
step before the AI.

**The automatic capability is genuinely absent, not merely unrouted.**
`AITask.SEGMENTATION` is configured `provider: NO_PROVIDER`, so
`POST /answer-scripts/{id}/segmentation` answers `503
segmentation_not_configured`; and even with regions stored, nothing converts
`document_regions` into `question_responses` -- `region_evidence.py` only READS
them as grading evidence. This is a missing pipeline stage, not a frontend
routing mistake, and it is the one thing between the current build and the
automatic flow.

**Expected cost when it does run** (from code, for this 5-question paper):
recognition batches answer images 5 per call, grading is one call per question
with a response, `max_retries = 2` (up to 3 calls each), `max_concurrency = 3`.
So roughly 1 recognition + 5 grading calls minimum, up to ~18 if everything
retried. `enqueue-processing` is NOT idempotent -- re-running re-grades and
overwrites marks; the only guard is the button's disabled state.

---

### Automatic answer preparation — DONE (AI-first path unblocked)

The stage between "a script was uploaded" and "there is something per question
to grade". Without it `enqueue_processing` refused every automatic run and the
only way forward was the student cutting their own paper up in the crop editor.

**The whole design is one observation:**
`GradingEvidence.has_student_evidence` is `bool(student_answer_text or
student_images.has_any)`. A response carrying recognised TEXT is already enough
for the entire grading pipeline -- so closing the gap needs no segmentation, no
geometry, no crops, no new persisted format and not one line of grading
changed. Segmentation stays deliberately unbuilt (`AITask.SEGMENTATION` is
still `NO_PROVIDER`).

```
AITask.ANSWER_MAPPING          new task: read a WHOLE script, assign answers to
                               the exam's EXISTING questions
ai/prompts/recognition.py      build_answer_mapping_prompt(question_numbers)
ai/answer_mapping.py    NEW    the contract + deterministic gate. No IO, no DB,
                               no provider concept -- shaped like ai/segmentation.py
ai/services.py                 map_answer_script(): ONE call, via visible_pages()
grading/preparation.py  NEW    the DB-facing stage and its outcome vocabulary
tasks.py                       preparation runs first, and gates the rest
routers/routingTasks.py        readiness moved, not weakened
routers/geminiAPI.py           crop recognition skips a response with no crops
```

**The exam's questions are authoritative.** The canonical numbers are stated in
the prompt AND enforced after: a number the model returns that the exam does
not have is discarded and logged, never created. Exactly the rule the
hidden-PDF-text incident produced -- a five-question paper must not become
seven because a model said so.

**Omission is meaningful.** A question with no entry means "not attempted" and
gets NO row, so grading skips it and aggregation treats it as skipped -- the
existing semantics, unchanged. An entry with an EMPTY answer is rejected too:
"attempted but wrote nothing" is not something a mapping pass can assert, and
accepting it would turn a preparation gap into a zero.

**No fake rows to satisfy a gate.** Creating five empty responses so
`enqueue_processing` would pass just moves the vacuous-aggregation bug. If
preparation maps nothing, `_process_and_grade` RETURNS BEFORE AGGREGATING and
leaves the stage alone -- because `aggregate_student_result` finalises when
every response that EXISTS carries a mark, which is vacuously true of zero rows
and would stamp a fabricated `0.0`.

**Readiness moved one level up, it was not relaxed.** The route now refuses
only when there is nothing to read at all (no responses AND no script); the
invariant now lives where aggregation actually happens, which is strictly
stronger than a check at enqueue time.

**Never overwrites.** Preparation is a no-op the moment ANY response exists --
crop-built, teacher-corrected or already graded. That is what makes it safe to
call on every run with no unique `(student_id, question_id)` constraint to lean
on, and it is why legacy exams and the crop editor keep working untouched. No
schema migration was needed.

- 812 tests (762 + 50). `test_answer_preparation.py` (37) holds the gate and
  the stage: tolerant number shapes but strict identity, 33/38 discarded, empty
  and duplicate entries dropped, provider failure / unrenderable script /
  unusable JSON each writing nothing, an existing response never touched, one
  call per script, PDFs reaching the provider as PNG pages, and no student text
  in a log line. `test_offline_auto_pipeline.py` (10) runs the REAL Celery task
  body with only the vendor call stubbed: upload -> prepare -> grade ->
  aggregate -> stage, with no crop route called and no `ans_*_images` written.
- **Live, exam 4, one run:** `409 -> 200 "AI grading started."` **1
  answer_mapping call** for the whole 4-page script (39.7s, 1 attempt) + 7
  grading calls across 5 questions. Five responses created for exactly
  31/32/36/37/39, no duplicates, no phantom 33/38, reference side untouched.
  Q32/Q36/Q37/Q39 graded 3/3, 5/5, 4/4, 4/4. **Q31 exhausted its 3 attempts and
  the response failed to parse:** mark stayed `NULL` with
  `grading_error_code=malformed_json`, siblings unaffected, result
  `grading_incomplete` with `graded_at` NULL and stage left at
  `EXAM_STAGE_GRADING` (6). No false finalisation. `/exams/4/stats` returns the
  neutral code plus "The grading response was not valid JSON." -- no provider
  text, no traceback. Zero occurrences of any stored answer in the logs.

**Open:** Q31 is the longest answer (829 chars, the multi-part diagram
question) and is the one whose grading response would not parse three times
running. Worth one look at whether it is length, the diagram description, or
just variance before the full paper is attempted.

---

### Q31 `malformed_json` — diagnosed, no parser defect

**The earlier summary of this failure was wrong**, and reading the run's own
telemetry rather than the persisted code is what corrected it:

```
12:53:01  provider error, retrying: task=grading category=timeout attempt=1/3
12:55:02  provider error, retrying: task=grading category=timeout attempt=2/3
12:56:38  ai_invocation task=grading ... attempts=3 success=True duration_ms=336491
```

Attempts 1 and 2 were **timeouts**, not malformed responses. Only attempt 3
returned a body, and only that body failed to decode. Q31's siblings took
69-109s against a 120s budget; Q31 twice ran the clock out and succeeded on the
third try at ~95s. Attribution is from timing (question_id=68, grading
concurrency 3), because the retry line carries no question id.

**`malformed_json` means exactly one thing** (`grading/result.py`): the text
contained something object-shaped and `json.loads` failed on it. Schema and
range problems have their own codes -- `wrong_schema`, `score_missing`,
`score_not_numeric`, `score_not_finite`, `score_negative`, `score_above_max` --
and the decoder was characterised offline over 48 cases to prove they are not
being absorbed into it. **No parser defect was found.** It accepts fenced
blocks, leading prose, `"2.5"` as a string, extra fields, a genuine `0` and
fractions; it rejects, with the right code each time, unterminated JSON,
trailing commas, single quotes, NaN/Infinity, negative and above-max scores.
`not_json` is the one raised code with no professor-facing sentence; it
degrades to the generic line, which is now asserted rather than assumed.

**The real defect found: the provider's finish reason was thrown away.**
`response.text` returns the PARTIAL body when generation stops at an output
limit (verified in the installed google-generativeai 0.8.4 source: it raises
only when `parts` is empty). So a truncated answer reaches the decoder as
ordinary invalid JSON and is recorded as `malformed_json` -- the same code a
model writing nonsense gets. The provider knew which it was; the adapter kept
neither the reason nor the token counts.

Fixed, diagnostics only, no behaviour change to grading:

```
ai/contracts.py           FinishReason (complete/truncated/blocked/other/unknown)
                          + finish_reason, input_tokens, output_tokens on ProviderResponse
ai/providers/gemini.py    _finish_reason() / _token_counts(); a truncated response
                          adds a `truncated_response` warning and a WARNING line
ai/services.py            carries them through run_task
ai/telemetry.py           logs finish_reason= and output_tokens=
```

Provider-neutral by construction: the vendor enum is spoken in the adapter and
nowhere else, and a test walks every non-adapter module asserting no
`MAX_TOKENS`/`RECITATION`/`SPII` string escapes it. Both helpers are
exception-proof -- a diagnostic that can fail a working grading call is worse
than no diagnostic.

**Also corrected: a factually wrong comment.** The adapter said `response_schema`
"wants a version-specific SDK type", justifying its absence. The pinned 0.8.4
declares `protos.Schema | Mapping[str, Any] | type | None`, and a plain dict was
verified locally to convert (no API call). `response_mime_type:
application/json` IS already a real provider-level JSON mode; constraining the
SCHEMA as well is available and is the obvious next step, but it changes what
the model may emit on every graded question, so it is left for a change that can
be validated against the provider.

**Classification: `PROVIDER_RESPONSE_VARIANCE`, with an `INSUFFICIENT_TELEMETRY`
caveat now closed.** No deterministic application defect explains Q31; two
timeouts plus one undecodable body on the paper's longest answer is consistent
with a slow, verbose generation, but truncation is NOT proven -- and could not
have been, because the reason was discarded. It can be next time.

- 860 tests (812 + 48). `test_grading_response_contract.py` pins the decoder's
  accept/reject behaviour case by case, the vendor-to-domain finish-reason
  translation, exception-proofing, provider neutrality, and one end-to-end
  truncation through the real adapter with the SDK call stubbed -- asserting the
  response body never reaches a log line. **Zero provider calls; exam 4 was NOT
  re-run.**

**Length/diagram is correlation only.** Q31 is the longest mapped answer (829
chars) and the multi-part diagram question, but `grading_evidence` shows
`buckets={}` and `files=0` for every question in that run -- no images were
attached to any grading call, Q31 included. So "diagram-heavy" cannot be the
mechanism; the reference diagram was never in the request.

---

### Q31 targeted retry — `GRADING_TIMEOUT`, and two corrections

**Correction 1 — the question ids were mis-attributed.** Exam 4's rows are
`67=Q31, 68=Q32, 69=Q36, 70=Q37, 71=Q39`. The previous note read the first
run's telemetry against the wrong mapping. Re-derived from the database:

```
question_id=67  Q31  attempts=1  69.7s   success=True  -> body failed to decode
question_id=68  Q32  attempts=3  336.5s  success=True  -> graded 3.00
```

So the two `category=timeout` retries belonged to **Q32**, which then
succeeded. **Q31's original failure was a single attempt that returned a body
in ~70s and would not parse.** Both earlier accounts of this were wrong; this
one comes from the ids in `questions`.

**Correction 2 — never target Q31 by id 68.** That is Q32, already graded.

**The targeted run.** One HTTP operation, `POST /grade-question-with-diagram`
with `question_id=67`, chosen because it is the only route that reproduces the
failing path faithfully: it READS the stored `QuestionResponse`, builds evidence
through `build_region_aware_evidence`, uses `_build_diagram_prompt_parts` and
`grade_answer_with_parts`, touches no sibling, creates nothing, and does not
aggregate. Retries apply, so one operation is up to three provider attempts.

```
14:31:02  evidence built: question_id=67 evidence_source=legacy_crops buckets={} (text only)
14:33:02  provider error, retrying: category=temporary  attempt=1/3  delay=0.22s   (~120s)
14:35:02  provider error, retrying: category=timeout    attempt=2/3  delay=1.46s   (~120s)
14:37:03  ai_invocation task=grading attempts=3 success=False error_category=timeout
          duration_ms=360382 files=0 question_id=67
```

**All three attempts hit the 120s wall clock. No response was ever returned**,
so `finish_reason` and the token counts could not be observed -- they are read
off a response object, and there was none. The new diagnostics are therefore
still unexercised in the field; they will report on the first attempt that
comes back.

**Classification: `GRADING_TIMEOUT`.** Q31's grading call is simply slow. Across
both runs its siblings finished in 69-109s against a 120s budget, and Q31 has
now spent 70s (returning unparseable output) and then 3 x 120s returning
nothing. It is the paper's longest mapped answer (829 chars) and the request is
TEXT ONLY -- `files=0`, `buckets={}`, no images on any grading call -- so the
budget, not the payload, is what it keeps hitting.

**Nothing else moved.** The only change in the whole database is Q31's
`grading_error_code`: `malformed_json` -> `timeout`. The siblings' fingerprint
(marks, error codes, reasoning, answer lengths) is byte-identical; no duplicate
responses, no new questions, no Q33/Q38, no reference-side contamination, no
remapping (`ans_text_images` still NULL). Exam stage stays 6, result stays
`grading_incomplete` at 16.00 with `graded_at` unset -- correct, and the route
does not aggregate.

**Cost: 1 HTTP operation = 3 provider attempts, 2 retries, 0 recognition or
mapping calls, 0 sibling grading calls.**

**Defect found here, FIXED later** (see *Reliability wrap-up* below): `POST /exam/{id}/question/{qid}/student/{sid}/reevaluate`
cannot be used on an automatically prepared response. It calls
`extract_single_answer_text`, which does `json.loads(qr.ans_text_images)` and
guards only `json.JSONDecodeError`; an auto-prepared row has that column NULL,
so `json.loads(None)` raises `TypeError` and the route 500s -- after it has
already nulled `marks_obtained` and committed. For Q31 nothing was lost (the
mark was already NULL), but on a GRADED question that route would clear the
mark and then die before restoring it. It was avoided for exactly this reason.

---

### Q31 single-attempt diagnostic — `GRADING_TIMEOUT` is WRONG, it is malformed JSON

**One provider attempt, no retries, a 180s budget — and it answered in 16
seconds.** That overturns the previous section's classification.

Configuration supported the whole diagnostic with no code change.
`get_task_settings` resolves `CG_AI__<TASK>__<FIELD>`, and its override list
already contains both `timeout_seconds` and `max_retries`; `max_retries` has no
clamp (only `max_concurrency` does), and `run_with_retries` computes
`total = max(0, max_retries) + 1`, so `0` really means one attempt. The
overrides were injected as a compose file kept OUTSIDE the repository and the
backend recreated with it; `.env` was never touched (`load_dotenv()` defaults to
`override=False`, so process environment wins anyway). The running backend was
made to print its effective settings BEFORE the call: grading
`timeout=180.0 retries=0`, every other task unchanged.

```
07:09:27  grading_evidence question_id=67 evidence_source=legacy_crops buckets={} (text only)
07:09:44  ai_invocation task=grading attempts=1 success=True error_category=- files=0
          duration_ms=16055 question_id=67
07:09:44  grading failed: code=malformed_json question_id=67
          detail=response was not valid JSON: Extra data: line 1 column 407 (char 406)
```

**`attempts=1`, 16.1s, `success=True`.** The provider returned a complete body
well inside a budget it had previously spent 120s failing to meet three times
running. Q31 is not slow. The earlier 120s walls were provider-side latency
variance on that day, not a property of this question, and
**`GRADING_TIMEOUT` should not be carried forward as the diagnosis.**

**"Extra data" is the whole finding.** The raw body was 407 characters. A
complete JSON object ends at char 406 and exactly ONE further non-whitespace
character follows it. `json.loads` reports that as `Extra data`; a response cut
off at an output limit cannot produce it — truncation raises `Unterminated
string` or `Expecting value` on an object that never closed. So the response was
COMPLETE and carried one stray trailing character. Output budget and schema size
are not implicated, and 407 characters is nowhere near any token ceiling.

**Where the parser lets it through.** `_extract_json_object`
(`backend/grading/result.py`) strips a fenced block only when the text STARTS
with a fence. This body starts with `{`, so the function returns the entire
stripped text unchanged and the `_JSON_OBJECT` regex fallback — which would have
matched just the object and dropped the trailing character — is never reached.
That asymmetry is the narrow defect. **Deliberately not fixed in this
diagnostic**, and it is a parser-precedence fix, not a loosening: the object is
still validated strictly afterwards.

**Diagnostics still unexercised.** `finish_reason` and the token counts could
not be reported, and this time NOT because no response arrived. The running
image was built 2026-09-02, before those changes were written; the container has
no `finish_reason` in `ai/telemetry.py`, no `input_tokens` in `ai/contracts.py`
and no `_token_counts` in `providers/gemini.py`. Reading them costs an image
rebuild plus one more provider call. The `Extra data` position is conclusive on
its own, so that call was not spent.

**Cost: 1 HTTP operation = exactly 1 Gemini call.**

**Nothing else moved.** The only database change is Q31's `grading_error_code`:
`timeout` -> `malformed_json`. Marks stayed NULL — no fabricated zero. The four
siblings' fingerprints (marks, error codes, reasoning, answer text, image
columns) are byte-identical before and after: Q32 3.00, Q36 5.00, Q37 4.00,
Q39 4.00. Five responses, no duplicates, no phantom 33/38, `ans_text_images`
still NULL (no remapping), exam stage 6, result `grading_incomplete` at 16.00
with `graded_at` unset. Grading configuration was restored to `timeout=120.0
retries=2` and verified from inside the running backend.

**Confirmed ids** (re-read from `questions` again this run):
`67=Q31, 68=Q32, 69=Q36, 70=Q37, 71=Q39`. Q31's response row is id 40.

---

### Q31 parser fix — structural JSON boundary, and Q31 graded 3.00

**The `malformed_json` was a parser-boundary defect, not a provider defect.**
The single-attempt diagnostic above showed a complete body in 16.1s that
`json.loads` refused with `Extra data: line 1 column 407 (char 406)`. That error
is only producible by a body that CLOSED — truncation raises `Unterminated
string` or `Expecting value` on an object that never ended — so the provider had
returned a complete grading object followed by one stray character.

**The old control flow could never reach its own fallback.**
`_extract_json_object` stripped a leading fence, then:

```
if stripped.startswith("{"):  return stripped        # <- whole text, stray char and all
match = _JSON_OBJECT.search(stripped)                # <- unreachable for those bodies
```

The regex fallback that would have isolated the object only ran when the text
did NOT start with an object. Every body that needed it took the early return.

**The fix is a boundary fix, not a loosening.** `json.JSONDecoder.raw_decode`
consumes exactly ONE complete JSON value from a given index and reports where it
ended; that end position is what separates "a complete object with something
harmless after it" from "a broken object". No regex brace-matching — a greedy
pattern runs across nested objects, a lazy one stops inside them, and both are
blind to braces inside strings. **No score is ever read out of prose.** The
decoded object still goes through the unchanged strict schema / numeric /
finite / non-negative / max-marks validation.

```
backend/grading/result.py   _JSON_OBJECT + _extract_json_object  ->
                            _DECODER, _object_starts, _decode_first_object,
                            _contains_another_object, _decode_json_object
backend/grading/failure.py  + "ambiguous_json" professor-facing sentence
```

**Trailing-data policy.** Accepted: whitespace, a fenced block, prose before the
object, and harmless non-JSON material after it. Refused as `ambiguous_json`: a
second DECODABLE JSON OBJECT anywhere after the first, because two structured
payloads make the response ambiguous and picking one would be a guess. The
ambiguity check looks for objects only — a trailing sentence such as "Total: 3
marks" contains a perfectly decodable JSON *number*, and rejecting on that would
throw a valid grade away over ordinary prose. Codes are otherwise unchanged: a
broken first object is still `malformed_json`, no object-looking start at all is
still `not_json`.

- **910 tests (860 + 50), all passing.** `test_trailing_data.py` holds the
  contract: the live SHAPE (complete object + one stray character) accepted,
  trailing prose accepted, trailing prose containing a bare number accepted,
  nested objects and braces inside string values accepted (a regex would have
  broken all of these), genuine zero and 0.5/1.5/2.25 preserved through trailing
  data, truncation still `malformed_json`, two objects rejected as
  `ambiguous_json`, and no failure message quoting the provider body. One
  pre-existing characterisation assertion in `test_grading_response_contract.py`
  pinned the defect (`... "extra": "junk"} garbage` -> `malformed_json`) and was
  deliberately flipped to the accepted case.

**Live retest, Q31, one attempt.** Backend and celery worker rebuilt from the
working tree first, so container and source agree (this had NOT been true during
the previous diagnostic — the image predated the telemetry work).

```
07:24:54  ai_invocation task=grading attempts=1 success=True error_category=-
          duration_ms=8098 files=0 finish_reason=complete output_tokens=66
          question_id=67
```

**Q31 now grades 3.00 / 3.00, `grading_error_code` cleared to NULL.** This is
also the first time the finish-reason and token diagnostics have reported from
the field: `finish_reason=complete`, `output_tokens=66` — no truncation, and far
below any output ceiling, which independently confirms the earlier reading.

**Stated honestly:** this particular body parsed cleanly, so the live run does
not prove trailing material was present this time. What it proves is that Q31
grades. The parser fix is verified against the exact failure shape offline; the
body was deliberately not inspected.

**Cost: 1 HTTP operation = exactly 1 Gemini call.** Grading configuration was
restored to `timeout=120.0 retries=2` and verified from inside the running
backend.

**Siblings untouched:** Q32/Q36/Q37/Q39 fingerprints byte-identical across the
whole task (3.00 / 5.00 / 4.00 / 4.00). Five responses, no duplicates, no
phantom 33/38, `ans_text_images` still NULL (no remapping).

**NOT finalised, and correctly so.** All five responses now carry marks, but
`/grade-question-with-diagram` does not aggregate: `exam_results` still reads
16.00 / `grading_incomplete` / `graded_at` NULL, and exam stage is still 6. The
supported finalisation path is `POST /exam/{exam_id}/add-result`
(manager-only) -> `add_exam_result_internal` -> `aggregate_student_result`,
which recomputes the total from the response rows and stamps `graded_at` only
when every existing response carries a validated mark. Fully response-graded is
not the same as finalised, and the result must come from that path rather than
be written by hand.

---

### Exam 4 finalisation — result is final at 19.00/19.00, exam_stage is not

**The full AI-first path is now closed end to end for exam 4 / student 1.** All
five responses graded (Q31 3.00, Q32 3.00, Q36 5.00, Q37 4.00, Q39 4.00 = 19.00
of 19.00), then the supported aggregation path was invoked ONCE. **No provider
call, no regrade, no remap.**

**The route, verified from source before calling it.** `POST /exam/{exam_id}/add-result`
(externally `/api/exam/4/add-result`; the router carries no prefix and nginx
rewrites `^/api/(.*)$`). `student_id` is a **Form field, not JSON**. Authorised
by `require_exam_manager` plus `get_current_user_required`. It delegates to
`add_exam_result_internal`, which reads the exam's `Question` ids and the
student's `QuestionResponse` rows, calls `aggregate_student_result`, and writes
exactly ONE `ExamResult` row. **It never touches a QuestionResponse, and it
never writes a mark** -- it only totals what grading already stored.

```
FINALIZE 200  complete=true  is_final=true  status=graded  graded_count=5
              marks_obtained=19.0  ungraded_question_ids=[]
              graded_at=2026-09-03T07:30:17Z
```

Database after: `exam_results` id 1 -> `marks_obtained=19.00`, `status=graded`,
`graded_at` set. One result row, five response rows, and **all five response
fingerprints byte-identical before and after** -- aggregation aggregated and
changed nothing else.

**Completeness is decided, not assumed.** `aggregate_student_result` marks a
result complete when every response that EXISTS carries a non-NULL mark; a
question with no row at all is reported separately and does not block, so a
genuinely unattempted question cannot make an exam permanently unfinalisable.
`status` is then `graded` / `grading_incomplete` and `exam_result_is_final`
reads `status in ExamResultStatus.FINAL`, which is `("graded",)`. C6 holds:
`marks_obtained == 0` counts as a grade, only `None` is absent, and nothing is
cast to int so fractional marks total exactly. Held by
`test_exam_aggregation.py` (23 passing): zero is a grade, a single missing mark
blocks finalisation, a missing mark is not counted as zero, fractional marks are
not truncated, and a finalised result can be DEMOTED by a later failure.

**Read contracts verified live, backend-side, not from the UI:**

```
GET /exams/4/stats              status=graded is_final=true total_marks=19.0
   (manager)                    percentage=100.0 grading_failures=[]
                                grading_progress=1.0 excluded_from_distribution=0
                                distribution puts the student in the 19.0 bucket
GET /exams/4/submission_status  status=graded is_final=true prepared=true
   (the student's own session)
GET /exam/4/student-evaluation/1  5 rows, marks summing to 19.0, every
                                  grading_error_code NULL, Q31 3.0/3.0
```

No NULL-as-zero anywhere: the 19.00 is derived from the stored
`QuestionResponse` marks, and `exams.points_possible` is 19.00, matching
`sum(questions.max_marks)`.

**GAP, found and deliberately not patched: `exam_stage` stays 6.**
`add_exam_result_internal` does not touch the stage; the ONLY place stage 7
(`EXAM_STAGE_GRADED`) is written is `tasks._process_and_grade`, which calls
`add_exam_result_internal` then `exam_result_is_final` then `set_exam_stage`.
Finalising through the route alone therefore leaves a correct, final
`ExamResult` beside an exam still reading `EXAM_STAGE_GRADING`. Nothing that
reports the RESULT is wrong -- stats, submission status and evaluation all read
from `ExamResult`/`QuestionResponse` and all say graded -- but any surface that
reads `GET /exams/{id}/stage` will still say grading. The supported writer is
`POST /exams/{exam_id}/stage` (manager-only, `exam_stage` as a query
parameter). This is the same exam-wide-vs-per-student mismatch already noted in
`tasks.py`, surfaced here as a concrete consequence rather than a theory. It
was NOT set by hand: a stage written manually would be a claim the aggregation
did not make.

---

### Reliability wrap-up — re-evaluation can no longer destroy a mark, stage follows result

**Bug 1 — `/reevaluate` erased grades.** All three re-evaluation routes opened
with `marks_obtained = None` + `commit()`, then called
`extract_single_answer_text`, which did `json.loads(qr.ans_text_images)` guarded
only by `json.JSONDecodeError`. Every automatically prepared row has that column
NULL, so `json.loads(None)` raised **`TypeError`** -- a different exception --
which escaped past the restore branch. A correctly graded answer was left with
no mark and the professor got a 500. Exam 4's five responses were all in exactly
that shape. Reproduced in a test against the pre-fix code before fixing.

Two layers, because one was not enough:

```
geminiAPI.extract_single_answer_text   NULL/blank ans_text_images now returns
                                       "Text extraction skipped" BEFORE decoding,
                                       matching what the batch path already did;
                                       the decode catch widened to (TypeError, ValueError)
examStats._reevaluate_one_response NEW  one helper, used by all three routes
```

**The rule is now: nothing is cleared until a replacement is validated.** The
helper snapshots mark, reason and failure code, runs extraction + grading inside
`try`, writes the new grade only on `status == "graded"`, and restores all three
fields verbatim on every failure path -- returned failure *or* raised exception.
Restoring the failure code too matters: the inner grading route persists its own
`grading_error_code`, so without it a valid mark could end up beside a stale
failure code. Semantics unchanged: a genuine 0 is a valid replacement (the test
is the status string, never truthiness of the score), fractional marks pass
through, a failure never writes a score, and the professor gets the safe
`describe(code)` sentence -- never provider text.

The three bulk/`all_students` routes were rewired to the same helper rather than
left exposed; that was a substitution, not a redesign.

**Bug 2 — a graded result beside a "grading" exam.** `add_exam_result_internal`
finalised the RESULT but never touched `exam_stage`; only
`tasks._process_and_grade` wrote stage 7. Finalising through the supported route
therefore left `status=graded` with `graded_at` set next to an exam still
reporting stage 6. The stage now follows the aggregation's own verdict, in the
same place the result is written:

```
if aggregation.complete:            -> EXAM_STAGE_GRADED
elif exam.exam_stage == GRADED:     -> EXAM_STAGE_GRADING   (demotion only)
```

Promotion is impossible for an incomplete run, and the demotion is deliberately
narrow -- only an exam already marked Graded moves back, so a paper that has not
reached grading is not dragged forward to stage 6. Still exam-wide while the job
is per-student (unchanged, see `tasks.py`).

- **923 tests (910 + 13), all passing.** `test_reevaluation_safety.py` holds
  both fixes: re-evaluating a NULL-`ans_text_images` row does not crash, a
  returned failure keeps the previous mark, a RAISED exception keeps it too
  (the regression itself), success replaces mark/reason and clears the code, a
  genuine 0 is a valid new grade and a stored 0 survives a failure, a fractional
  mark survives, no provider text reaches the message; and for the stage: a
  final result reaches GRADED, a zero total still reaches GRADED, an incomplete
  result never does and is not dragged forward, a later failure demotes only
  from GRADED, and a fractional total finalises exactly.

**Live, exam 4, no provider call.** Backend and worker rebuilt from the working
tree, both fixes confirmed present in the container. `POST /exam/4/add-result`
invoked once: `complete=true is_final=true marks_obtained=19.0 graded_count=5`,
and the database now reads **`exam_results` 19.00 / `graded` / `graded_at` set
AND `exams.exam_stage = 7`.** The five response marks (3/3/5/4/4) and their
cleared error codes are untouched. The result and the stage finally agree.

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

These were discovered while implementing earlier phases and are out of scope
until their own remediation phase. Items fixed in Backend Reliability v1 have
been removed from this list.

- **Account deletion has no retention policy of its own.** Deleting a user
  removes everything that references them, because every FK to `users.id` is
  `ON DELETE CASCADE` — including a professor's classrooms and every student's
  exam results inside them, and including notifications the user sent. The
  pre-fix code hinted at anonymising senders instead (`sender_id = None`) but
  never ran. If academic records should outlive an account, that needs a
  deliberate policy and `ON DELETE SET NULL` migrations; it was not invented in
  Reliability v2.
- **Deleted accounts' uploaded files are orphaned.** Deletion removes the
  profile picture (`{user_id}.jpg`) but nothing under `uploads/` — answer
  scripts, crops and marking-scheme images stay on disk after their rows are
  gone. Related: uploaded Gemini files are never deleted either. A storage
  lifecycle is its own phase.
- **Sessions are not invalidated on deletion.** A JWT already issued to the
  deleted user stays syntactically valid until it expires, but every
  authenticated request re-loads the user row, which no longer exists, so the
  auth dependency returns 401. That is adequate but incidental, not a deliberate
  revocation mechanism.
- **Announcement notifications are not sent.** The live
  `classes.create_announcement` never created them; the copy that did was the
  shadowed duplicate removed in Reliability v1. Starting to send them is a
  behaviour change, not a bug fix, so it was reported rather than ported.
- **TAs cannot remove students.** The shadowed
  `peopleManagement.remove_student` allowed it (`require_owner=False`); the
  live `enrollments.remove_student` is owner-only and always has been. Nothing
  changed — but if TAs should have this, decide it deliberately.
- **Re-evaluation is still serial.** `send_for_reevaluation` and the
  re-evaluate-all routes grade one question at a time. They could reuse
  `run_bounded`, but they were deliberately left alone: v2 scoped itself to the
  initial exam run, where the session/persistence split was the hard part.
- **The Gemini free tier cannot grade one paper: 20 requests/day/model.** The
  quota is `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, value 20, scoped
  to (project x model). A 33-question paper needs 33 calls, so no model choice
  or concurrency setting helps — the project needs billing enabled. Live
  validation lost 16 of 33 questions on a fresh sequential run and 33 of 33 by
  the third. `gemini-3.7-flash` is not quota-blocked but returns 503 "high
  demand" on every attempt, so it is not a workaround either.
- **Grading quality on `gemini-3.6-flash` is unbenchmarked.** The default was
  changed because the previous model was withdrawn, not because the new one was
  evaluated.
- **`backend/database.py` sets `echo=True`**, logging every SQL statement --
  including model-written grading reasons about student answers -- and it
  crashed the logging handler on a non-ASCII character during live validation.
- **Nothing produces regions in production yet.** The only segmentation
  adapter is the deterministic fake used by tests; `get_segmentation_provider`
  has no default, so a real one must be written and named explicitly. The crop
  editor still writes crops, not regions.
- **Marking schemes have no structured regions.** Reference evidence still
  comes only from `Question.ms_*_images`; the region model supports materials
  but nothing populates or reads them.
- **`math` regions ride in the diagram bucket** (see Region Evidence v1). The
  grading prompt therefore describes handwritten mathematics as a diagram until
  an HMER stage exists.
- **The crop editor still writes crops, not regions.** Structured evidence only
  engages for exams somebody has annotated through the region API.
- **Grading concurrency is per process, not per deployment.** The semaphore
  bounds one exam run. Two Celery workers grading two students simultaneously
  are each bounded to 3, so the real ceiling is `workers × 3`. A distributed
  limiter is deliberately not built; the per-task setting is the control.
- **Remaining AI coupling:** the router still owns question-label parsing,
  batching (chunks of 5) and marking-scheme key mapping — orchestration that a
  later phase should move behind the service layer. Two commented-out legacy
  blocks in `geminiAPI.py` still mention the SDK.
- **`process_marking_scheme_text_image` is still unexercised end to end.** It
  no longer crashes on entry (Reliability v1) and now runs through the service
  layer, but proving it needs a live provider call.
- ~~`tasks.py` advances the exam to stage 7 after grading regardless of
  outcome. That is correct as it stands — stage 7 is "grading started"...~~
  **REVERSED and FIXED** in Offline Pipeline Harness v1. The premise was wrong:
  `Exam.exam_stage`'s own comment lists eight labels for stages 0–7, so 6 is
  "Grading" and 7 is "Graded". The stage is now conditional on the
  aggregation's verdict. The residual multi-student limitation is unchanged and
  still open — see that phase.
- `backend/routers/old/` is dead and does not compile. Excluded from
  `compileall` runs; it should simply be deleted at some point.

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
.venv-test/Scripts/python.exe -m pytest -q      # 647 passed
```

SQLite now enforces foreign keys (see Reliability v2), so cascade behaviour in
the suite matches PostgreSQL rather than silently doing nothing.

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

### Migration 0003 (Backend Reliability v1)

Also runtime-verified on PostgreSQL 15.19 in a disposable container: upgrade
`0002 -> 0003` added `question_responses.grading_error_code` as nullable text,
existing rows kept their marks (`3.50` stayed `3.50`, a NULL mark stayed NULL)
and defaulted to a NULL code, a code written afterwards read back, revision
reported `0003`, and an autogenerate comparison against the live database
showed no drift. On PostgreSQL this is a plain `ADD COLUMN`, which does not
rewrite the table.

---

### Account deletion (Backend Reliability v2)

Verified on PostgreSQL 15.19 in a disposable container, through the real route:
a wrong password left the account intact (400); a correct one returned 200 and
the user row was gone; all seven dependent tables cascaded to zero; a
bystander's user row, response and the classroom were untouched; deleting the
classroom OWNER additionally removed their classroom and its exam while leaving
the bystander intact. Exactly one user remained, as expected.

---

## Experiments (isolated, not production)

`experiments/` holds three provider-evaluation harnesses that are the reference
for the future provider-agnostic schema: `segmentation-v1`, `hierarchy-v1`
(question tree + OR-choice + marking-scheme alignment, scored clean on its
stress set), `seg-v1.1` and `seg-v11-lite` (recall hardening, page detection and
rectification for photographed sheets). Their `results/` and
`experiment-data/` are gitignored and contain student data — never commit them.

---

## MVP status

**COGNIGRADE MVP IS WORKABLE.** A small real exam goes from answer images
through AI recognition, grading, aggregation and a correct final result. The
remaining limit is commercial, not architectural: the free tier allows 20
requests per day per model per project, so a real 33-question paper needs
roughly 34 and cannot complete. Nothing in the code prevents it.

**Blocker to a full-size demo: enable billing on the Gemini project.** That is
a decision, not a task.

## Next approved phase

**Current phase: UI/demo polish.** UI-1, UI-2 and UI-3 are done (above). Next
approved step is **UI-4 — AI processing and grading status UX**: an instructor
who presses Start AI grading gets one line of confirmation and nothing further,
and a student who has submitted sees "awaiting grading" with no sense of whether
a run is under way. The state to render already exists —
`/exams/{id}/submission_status` returns `status`/`is_final`/`prepared` and
`/exams/{id}/stats` returns per-student `status`, `is_final` and
`grading_failures` — and `exam-stats.htm` already polls the latter every five
seconds, so this should be frontend-only. Do not fabricate percentages: the
backend reports stages and outcomes, not progress.

Recommended after the UI phase: **make an incomplete exam recoverable
without re-running the whole paper.** Re-execution is self-healing but
all-or-nothing: recovering one failed question re-runs recognition and grading
for every question, spending quota on work already done. On a 20/day cap that
is the difference between one recoverable mistake and none. The task is a
re-grade path that selects only the responses carrying a `grading_error_code`
(or no mark), grades those, and re-aggregates — reusing `grade_exam_logic`'s
three phases rather than adding a second orchestration. Zero new schema: the
failure-code column already carries the state.

Explicitly NOT recommended: region-editor development. Regions should arrive
from a segmentation model and deterministic validation, not from a drawing
task — see **What CogniGrade is**.

Still blocked on provisioning, separately: the Gemini free tier allows 20
requests per day per model, so no real end-to-end grading run — and no live
segmentation model — is possible until billing is enabled.

Still open as policy, deliberately untouched: account deletion cascades through
institutional academic data, and the repository tracks real profile-picture
files and answer-script PDFs committed before those paths were gitignored.

**FOUNDATION PR IS OPEN.** The complete real end-to-end grading run it was
waiting on happened (MVP Deployment & Live Demo v1), and the PR into `main` has
been created. It is not merged.
