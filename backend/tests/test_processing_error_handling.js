/*
  What the professor is told when a long processing request does not come back
  cleanly, and what the workflow does next.

  The bug these pin: `/extract-text` and `/extract-question-labels` run
  SYNCHRONOUSLY while the model reads a document. nginx gave up at its 60s
  default and answered 504 with an HTML page; `response.json()` threw on that
  HTML and landed in the same catch as a dropped connection, so the professor
  was told "Error connecting to server." while FastAPI carried on and committed
  the extraction. Worse, `extractTextForSection` swallowed the failure and
  returned normally, so `submitExam` went on to save files, process the question
  paper and stamp the exam as processed.

  The helper is lifted out of frontend/exam.htm and exercised directly: these
  are pure functions over a fetch Response, so they need no browser and no
  network. Run with:  node backend/tests/test_processing_error_handling.js
*/

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const vm = require("vm");

const EXAM_HTML = path.join(__dirname, "..", "..", "frontend", "exam.htm");

// ---------------------------------------------------------------------------
// load the helper out of the page, so the test cannot drift from the source
// ---------------------------------------------------------------------------

function loadHelper(authFetchImpl) {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  const start = html.indexOf("const PROCESSING_MESSAGES = {");
  const endMarker = "function processingMessage(err) {";
  const end = html.indexOf(endMarker);
  assert.ok(start > 0 && end > start, "helper block not found in exam.htm");
  const tail = html.slice(end);
  const source = html.slice(start, end) + tail.slice(0, tail.indexOf("\n    }") + 6);

  const sandbox = { authFetch: authFetchImpl, console: { error() {} } };
  vm.createContext(sandbox);
  vm.runInContext(source + "\nthis.__api = {PROCESSING_MESSAGES, ProcessingError, readJsonBody, processingFailureFor, requestProcessing, processingMessage};", sandbox);
  return sandbox.__api;
}

function fakeResponse({ status = 200, contentType = "application/json", body = "{}" }) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => JSON.parse(body),   // throws on HTML, exactly like fetch
    text: async () => body,
  };
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// ---------------------------------------------------------------------------
// the success path still works
// ---------------------------------------------------------------------------

test("a JSON 200 is returned to the caller", async () => {
  const api = loadHelper(async () => fakeResponse({ body: '{"results":[{"text":"ok"}]}' }));
  const data = await api.requestProcessing("/extract-text", { method: "POST" });
  assert.deepStrictEqual(data.results, [{ text: "ok" }]);
});

// ---------------------------------------------------------------------------
// the gateway timeout that started all this
// ---------------------------------------------------------------------------

test("a 504 HTML page is a timeout, not a connection failure", async () => {
  const api = loadHelper(async () => fakeResponse({
    status: 504, contentType: "text/html",
    body: "<html><head><title>504 Gateway Time-out</title></head></html>",
  }));
  await assert.rejects(
    () => api.requestProcessing("/extract-text", {}),
    (err) => {
      assert.strictEqual(err.category, "timeout");
      assert.strictEqual(err.message, api.PROCESSING_MESSAGES.timeout);
      assert.ok(!/network|connect/i.test(err.message), "must not blame the connection");
      return true;
    }
  );
});

test("the HTML body never reaches the professor", async () => {
  const html = "<html><body><center>nginx/1.25.3</center></body></html>";
  const api = loadHelper(async () => fakeResponse({ status: 504, contentType: "text/html", body: html }));
  await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
    for (const leak of ["<html", "nginx", "Gateway", "Unexpected token"]) {
      assert.ok(!err.message.includes(leak), `${leak} must not be shown`);
    }
    return true;
  });
});

test("parsing an HTML body is never even attempted", async () => {
  let jsonCalled = false;
  const api = loadHelper(async () => {
    const r = fakeResponse({ status: 504, contentType: "text/html", body: "<html></html>" });
    r.json = async () => { jsonCalled = true; throw new SyntaxError("Unexpected token '<'"); };
    return r;
  });
  await assert.rejects(() => api.requestProcessing("/x", {}));
  assert.strictEqual(jsonCalled, false, "content-type is checked before parsing");
});

test("502 and 503 are also treated as unknown outcomes", async () => {
  for (const status of [502, 503, 408]) {
    const api = loadHelper(async () => fakeResponse({ status, contentType: "text/html", body: "<html/>" }));
    await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
      assert.strictEqual(err.category, "timeout", `status ${status}`);
      return true;
    });
  }
});

// ---------------------------------------------------------------------------
// real failures stay real
// ---------------------------------------------------------------------------

test("a genuine transport failure says the server is unreachable", async () => {
  const api = loadHelper(async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
    assert.strictEqual(err.category, "network");
    assert.strictEqual(err.message, api.PROCESSING_MESSAGES.network);
    return true;
  });
});

test("a 4xx detail written by this app is shown verbatim", async () => {
  const api = loadHelper(async () => fakeResponse({
    status: 409,
    body: JSON.stringify({ detail: "This question paper already has student responses." }),
  }));
  await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
    assert.strictEqual(err.message, "This question paper already has student responses.");
    assert.strictEqual(err.category, "failed");
    return true;
  });
});

test("a 500 body is never echoed", async () => {
  const api = loadHelper(async () => fakeResponse({
    status: 500,
    body: JSON.stringify({ detail: "Traceback: asyncpg.ConnectionError at /app/backend/db.py" }),
  }));
  await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
    assert.strictEqual(err.message, api.PROCESSING_MESSAGES.failed);
    for (const leak of ["Traceback", "asyncpg", "/app/"]) {
      assert.ok(!err.message.includes(leak), `${leak} must not be shown`);
    }
    return true;
  });
});

test("a 200 whose body is not JSON is an unknown outcome, not a success", async () => {
  const api = loadHelper(async () => fakeResponse({ status: 200, contentType: "text/html", body: "<html/>" }));
  await assert.rejects(() => api.requestProcessing("/x", {}), (err) => {
    assert.strictEqual(err.category, "timeout");
    return true;
  });
});

test("processingMessage falls back safely for a non-ProcessingError", async () => {
  const api = loadHelper(async () => fakeResponse({}));
  assert.strictEqual(
    api.processingMessage(new SyntaxError("Unexpected token '<' in JSON at position 0")),
    api.PROCESSING_MESSAGES.failed
  );
});

// ---------------------------------------------------------------------------
// control flow: a required step that failed must stop the run
// ---------------------------------------------------------------------------

test("extractTextForSection throws instead of swallowing", () => {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  const start = html.indexOf("async function extractTextForSection(section)");
  const body = html.slice(start, html.indexOf("function openModal(section)", start));
  assert.ok(body.includes("throw err;"), "the failure must propagate to the caller");
  assert.ok(!body.includes('alert("Error connecting to server.")'),
    "the blanket connection message is gone");
  assert.ok(body.includes("requestProcessing("), "it must go through the classifier");
});

test("extractQuestionLabels throws instead of swallowing", () => {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  const start = html.indexOf("async function extractQuestionLabels()");
  const body = html.slice(start);
  assert.ok(body.includes("throw err;"), "the failure must propagate to the click handler");
  assert.ok(body.includes("requestProcessing("));
});

test("submitExam advances the stage only after every step returned", () => {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  const start = html.indexOf("async function submitExam()");
  const body = html.slice(start, html.indexOf("async function processQuestionPaper", start));
  const stageAt = body.indexOf("await setStage(6)");
  const catchAt = body.indexOf("} catch (err)");
  assert.ok(stageAt > 0 && catchAt > stageAt,
    "setStage(6) must sit inside the try, before the catch, so a throw skips it");
  assert.ok(body.includes('await extractTextForSection("Questions")'),
    "the required step is awaited, so its rejection unwinds submitExam");
});

test("the question-paper click handler advances only on success", () => {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  const start = html.indexOf("document.getElementById('processQpBtn').addEventListener");
  const body = html.slice(start, start + 2000);
  const callAt = body.indexOf("await extractQuestionLabels()");
  const stageAt = body.indexOf("await setStage(1)");
  const catchAt = body.indexOf("} catch (err)");
  assert.ok(callAt > 0 && stageAt > callAt && catchAt > stageAt,
    "setStage(1) must follow the extraction inside the same try");
});

test("no blanket connection message survives anywhere in the page", () => {
  const html = fs.readFileSync(EXAM_HTML, "utf8");
  assert.ok(!html.includes('alert("Error connecting to server.");\n        extractBtn'),
    "no live call site may still use it");
  const live = html.split("\n").filter(
    (line) => line.includes('"Error connecting to server."') && !line.trim().startsWith("catch (error)")
  );
  assert.deepStrictEqual(live, [], "only the explanatory comment may mention it");
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
