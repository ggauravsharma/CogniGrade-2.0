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
- `tasks.py` advances the exam to stage 7 after grading regardless of outcome.
  That is correct as it stands — stage 7 is "grading started", an exam-wide
  workflow marker, and result release is gated per student by
  `ExamResult.status` — but the exam stage alone never signals trouble.
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
.venv-test/Scripts/python.exe -m pytest -q      # 436 passed
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

## Next approved phase

Not yet approved. Recommended next: **measure grading against a real
PostgreSQL + Celery + Gemini run, at concurrency 1 and 3, on one real paper.**
Everything about v2 is proven against fakes: the semaphore bound, failure
isolation, ordering, session safety and the wall-clock shape. What is NOT known
is how Gemini's actual rate limiting responds to three concurrent grading calls
on the deployment's own quota — whether the full-jitter retry absorbs it, or
whether the default should be 2. That needs one controlled run with real
credentials and a real answer script, and it is the last thing standing between
this and a confident prototype demo.

Still open as policy, deliberately untouched: account deletion cascades through
institutional academic data, and the repository tracks real profile-picture
files. Both are recorded above.
