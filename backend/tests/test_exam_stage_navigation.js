/*
  The professor's exam page: what renders a stage, what may CHANGE one, and
  what the normal workflow no longer forces anybody through.

  Two defects these pin.

  1. `exam.htm` opened with  fetchExamStage() -> setStage(currentStage), and
     setStage POSTed. Opening or refreshing the page therefore WROTE the exam
     stage: a swallowed 403 for a student, and a pointless rewrite of the same
     value for a professor. The stage column recorded page views instead of
     workflow progress.

  2. Stages 3 and 4 injected crop-edit.htm and called initialise_crop_edit, so
     uploading a marking scheme was punished with a manual cropping session
     (Box Cut / Freehand Cut / Edit Order) before the exam could advance. That
     step writes `question.ms_*_images` -- OPTIONAL reference images. Grading
     reads `question.ideal_marking_scheme`, the TEXT that processMarkingScheme
     extracts from the uploaded document, and the exam that graded end to end
     had every ms_*_images column NULL.

  Static assertions over the shipped files: no browser, no network, no quota.
  Run with:  node backend/tests/test_exam_stage_navigation.js
*/

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const FRONTEND = path.join(__dirname, "..", "..", "frontend");
const read = (name) => fs.readFileSync(path.join(FRONTEND, name), "utf8");

const EXAM = read("exam.htm");
const STUDENT = read("student-edit.htm");
const ANNOUNCEMENTS = read("announcements.js");

/** The body of a named async function, up to the next top-level declaration. */
function functionBody(source, signature, endMarker) {
  const start = source.indexOf(signature);
  assert.ok(start > 0, `${signature} not found`);
  const end = endMarker ? source.indexOf(endMarker, start) : source.length;
  assert.ok(end > start, `end of ${signature} not found`);
  return source.slice(start, end);
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// ---------------------------------------------------------------------------
// rendering a stage must not write one
// ---------------------------------------------------------------------------

test("the page load path renders and does not advance", () => {
  const init = functionBody(EXAM, "document.addEventListener('DOMContentLoaded'", "const uploadQuestions");
  assert.ok(init.includes("renderStage(currentStage)"), "load must render");
  assert.ok(!init.includes("advanceStage("), "load must not advance");
  assert.ok(!init.includes("postExamStage("), "load must not POST the stage");
});

test("renderStage never posts a stage", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  assert.ok(!body.includes("postExamStage("),
    "renderStage is the read-only half; posting here is what made a refresh a write");
  assert.ok(!body.includes("advanceStage("), "rendering must not cascade into a transition");
});

test("advanceStage is the only thing that posts", () => {
  const occurrences = EXAM.split("postExamStage(").length - 1;
  assert.strictEqual(occurrences, 1, "exactly one call site may write the stage");
  const body = functionBody(EXAM, "async function advanceStage(stage) {", "async function renderStage");
  assert.ok(body.includes("postExamStage(stage)"), "and it is advanceStage");
  assert.ok(body.includes("renderStage(stage)"), "which then shows the new stage");
});

test("the old combined setStage is gone", () => {
  assert.ok(!/\bsetStage\s*\(/.test(EXAM),
    "no call site may still use the function that both rendered and wrote");
});

// ---------------------------------------------------------------------------
// only a successful action advances
// ---------------------------------------------------------------------------

test("question-paper processing advances only after extraction returned", () => {
  const handler = functionBody(
    EXAM, "document.getElementById('processQpBtn').addEventListener", "document.getElementById('questionLabelsDone')"
  );
  const call = handler.indexOf("await extractQuestionLabels()");
  const advance = handler.indexOf("await advanceStage(1)");
  const cat = handler.indexOf("} catch (err)");
  assert.ok(call > 0 && advance > call && cat > advance,
    "advanceStage(1) must sit after the extraction and before the catch");
});

test("reference-material processing advances to the student-script step", () => {
  const handler = functionBody(
    EXAM, "document.getElementById('finalProcessBtn').addEventListener", "</script>"
  );
  assert.ok(handler.includes("await advanceStage(5)"),
    "a marking scheme must lead to student scripts, not to a cropping stage");
  assert.ok(!handler.includes("advanceStage(3)") && !handler.includes("advanceStage(4)"),
    "the legacy crop stages are no longer a destination");
  assert.ok(!handler.includes("crop-edit.htm"), "and the cropper is not preloaded");
});

test("submitExam advances only after every required step returned", () => {
  const body = functionBody(EXAM, "async function submitExam()", "async function processQuestionPaper");
  const advance = body.indexOf("await advanceStage(6)");
  const cat = body.indexOf("} catch (err)");
  assert.ok(advance > 0 && cat > advance,
    "advanceStage(6) must be inside the try, so a throw skips it");
  assert.ok(body.includes('await extractTextForSection("Questions")'),
    "the required step is awaited, so its rejection unwinds submitExam");
});

// ---------------------------------------------------------------------------
// the cropper is out of the normal professor path
// ---------------------------------------------------------------------------

test("no stage injects the crop editor any more", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  assert.ok(!body.includes("initialise_crop_edit"), "no stage may open the cropping workspace");
  // The filename appears in the explanatory comment on the legacy branch, so
  // assert on the CALL rather than the word.
  assert.ok(!/fetch\(\s*['"]crop-edit\.htm/.test(body),
    "no stage may fetch the cropper markup");
  assert.ok(!body.includes("cropEditSection.innerHTML"),
    "no stage may inject it into the page");
});

test("exam.htm never calls initialise_crop_edit at all", () => {
  assert.ok(!EXAM.includes("initialise_crop_edit("),
    "the professor page has no entry point into the cropper");
});

test("Box Cut and Freehand Cut are not in the professor page", () => {
  for (const marker of ["Box Cut", "Freehand Cut", "Edit Order"]) {
    assert.ok(!EXAM.includes(marker), `${marker} must not appear in the normal professor journey`);
  }
});

test("the cropper files are still in the repository", () => {
  for (const name of ["crop-edit.htm", "crop-edit.js"]) {
    assert.ok(fs.existsSync(path.join(FRONTEND, name)), `${name} must be kept as fallback tooling`);
  }
  const cropper = read("crop-edit.htm");
  assert.ok(cropper.includes("Box Cut"), "the tool itself is unchanged");
});

test("the marking-scheme region endpoint is untouched", () => {
  const backend = fs.readFileSync(
    path.join(__dirname, "..", "routers", "studentEdit.py"), "utf8");
  assert.ok(backend.includes("question_response/{document_type}"),
    "the crop persistence route stays; only its normal-flow entry point is gone");
  assert.ok(backend.includes("ms_text_images"), "reference-image storage is not removed");
});

// ---------------------------------------------------------------------------
// the rest of the professor journey still works
// ---------------------------------------------------------------------------

test("the student-script stage still shows the upload/roster UI", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  assert.ok(body.includes("stage === 5"), "stage 5 is still handled");
  assert.ok(body.includes("uploadStudent.style.display = 'block'"));
});

test("legacy stages 3 and 4 render the student-script step, not a cropper", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  const branch = body.slice(body.indexOf("stage === 3 || stage === 4"), body.indexOf("stage === 5"));
  assert.ok(branch.includes("uploadStudent.style.display = 'block'"),
    "an exam already at 3 or 4 moves on rather than being sent back to cropping");
  assert.ok(!branch.includes("advanceStage("),
    "and is NOT silently rewritten on sight -- rendering stays read-only");
});

test("the grading stage still exposes Start AI grading", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  assert.ok(body.includes("stage === 6") && body.includes("loadGradingRoster()"));
  assert.ok(EXAM.includes('btn.innerText = "Start AI grading"'), "the control is unchanged");
  assert.ok(EXAM.includes("enqueue-processing"), "and still calls the manager-only route");
});

test("the graded stage still reaches the results page", () => {
  const body = functionBody(EXAM, "async function renderStage(stage) {", "/* ------");
  assert.ok(body.includes("stage >= 7") && body.includes("exam-stats.htm"));
});

// ---------------------------------------------------------------------------
// the student flow is not disturbed
// ---------------------------------------------------------------------------

test("students are still routed to their own page", () => {
  assert.ok(ANNOUNCEMENTS.includes("student-edit.htm"), "the student branch survives");
  assert.ok(ANNOUNCEMENTS.includes("canManageThisClass"), "routing is still by membership");
});

test("the student page still polls its read-only progress endpoint", () => {
  assert.ok(STUDENT.includes("/my-grading-status"), "student progress is untouched");
  assert.ok(!STUDENT.includes("initialise_crop_edit("), "and still bypasses the cropper");
  assert.ok(!STUDENT.includes("enqueue-processing"), "a student still cannot start grading");
});

// ---------------------------------------------------------------------------

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("  ok   " + name);
    } catch (err) {
      failed++;
      console.log("  FAIL " + name + "\n       " + err.message);
    }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
