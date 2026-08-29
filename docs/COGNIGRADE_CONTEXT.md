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
- **C6** `examStats.py:548` sums only non-NULL marks and then stamps
  `status="graded"`, so a failed grading silently scores zero.
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
- Still open in the grading path after Correctness v1: free-text `Grade:` /
  `Reason:` parsing (a parse failure skips the DB write and leaves NULL);
  `examStats.py:548` treating NULL as zero and still stamping `status="graded"`;
  integer marks columns; grading is serial, one Gemini call per question;
  uploaded Gemini files are never deleted (`upload_file` used, `delete_file`
  never) — a diagram/table call now uploads reference + student images per
  question and none are cleaned up.
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
.venv-test/Scripts/python.exe -m pytest -q      # 83 passed
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

Not yet approved. Recommended next: **structured grading output** — replace the
free-text `Grade:` / `Reason:` parsing with a schema-validated result and make a
parse failure an explicit error rather than a silent NULL, which is the
remaining half of the wrong-marks problem (C6/C7).
