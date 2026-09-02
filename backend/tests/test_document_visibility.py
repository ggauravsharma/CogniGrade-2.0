"""What the AI is allowed to see, and what replacing a question paper does.

Two separate faults, found together while a five-question paper produced seven
questions on screen.

1.  VISIBILITY. The paper (31, 32, 36, 37, 39) had been cut down from a full
    one, and the questions that were cut were made invisible rather than
    deleted. MediaBox, CropBox and TrimBox were identical, so nothing sat
    outside a crop; the band where question 33 had been rendered with ZERO
    non-white pixels; and the text layer still held questions 33 and 38 in
    full. Ingestion sent the PDF itself to the provider -- `mime_type
    application/pdf`, all 489563 bytes -- so the provider read a document
    strictly richer than the one the teacher approved, and both invisible
    questions became rows in `questions`.

2.  REPLACEMENT. `/extract-question-labels` only ever appended. Re-processing a
    corrected paper kept every question the old one had; the only delete was in
    the browser, behind a filename-and-size comparison, in a separate request.
    That did not cause this incident -- all seven rows were written 45ms apart
    by one run -- but it would have caused the next one.

Everything here is offline: PDFs are built byte by byte in the test, rendering
is the real pypdfium2 path, and the provider is a recorder. No key, no network,
no quota.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.future import select

from backend.models.tables import Question, QuestionResponse
from backend.models.users import User
from backend.tests.conftest import REPO_ROOT, as_user

pdfium = pytest.importorskip("pypdfium2")


# ---------------------------------------------------------------------------
# a PDF that shows less than it contains
# ---------------------------------------------------------------------------

def _build_pdf(objects: list[bytes]) -> bytes:
    """Assemble numbered objects into a valid PDF with a real xref table."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at,
    )
    return bytes(out)


def paper_with_hidden_question(path, *, visible="31. Visible question",
                               hidden="33. Hidden question") -> str:
    """One A4 page: `visible` drawn normally, `hidden` drawn in invisible mode.

    `3 Tr` is the PDF text rendering mode meaning "fill nothing, stroke
    nothing" -- the text object is fully present, fully positioned INSIDE the
    page, and paints no pixels. That is the shape of the real paper: the
    content was hidden, not removed, and no box was changed.
    """
    stream = (
        b"BT /F1 24 Tf 0 Tr 60 700 Td (" + visible.encode("ascii") + b") Tj ET\n"
        b"BT /F1 24 Tf 3 Tr 60 400 Td (" + hidden.encode("ascii") + b") Tj ET\n"
    )
    pdf = _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
    ])
    path = str(path)
    with open(path, "wb") as handle:
        handle.write(pdf)
    return path


def _ink_in_band(image, y_top_pt, y_bottom_pt, page_height_pt=842.0):
    """Count non-white pixels in a horizontal band given in PDF points."""
    grey = image.convert("L")
    width, height = grey.size
    scale = height / page_height_pt
    top = int((page_height_pt - y_top_pt) * scale)
    bottom = int((page_height_pt - y_bottom_pt) * scale)
    band = grey.crop((0, max(top, 0), width, min(bottom, height)))
    return sum(1 for value in band.getdata() if value < 250)


# --- the fixture itself has to be the real thing --------------------------

def test_the_fixture_reproduces_the_real_paper(tmp_path):
    """Otherwise the rest of this file proves nothing about the live failure."""
    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    document = pdfium.PdfDocument(path)
    page = document[0]

    assert page.get_mediabox() == page.get_cropbox(), "no crop, exactly as in the real paper"

    text = page.get_textpage().get_text_bounded()
    assert "31. Visible question" in text
    assert "33. Hidden question" in text, "the hidden text must survive in the text layer"

    image = page.render(scale=2.0).to_pil()
    assert _ink_in_band(image, 725, 690) > 0, "the visible question must draw pixels"
    assert _ink_in_band(image, 425, 390) == 0, "the hidden question must draw none"


# ---------------------------------------------------------------------------
# normalising a document to its visible pages
# ---------------------------------------------------------------------------

def test_a_pdf_becomes_one_image_per_page(tmp_path):
    from backend.ai.documents import render_visible_pages

    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    out = tmp_path / "pages"
    out.mkdir()

    pages = render_visible_pages(path, str(out))
    assert len(pages) == 1
    assert pages[0].endswith(".png")
    with open(pages[0], "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n", "not actually a PNG"


def test_the_rendered_page_does_not_carry_the_hidden_question(tmp_path):
    """The whole point: what survives normalisation is what a reader sees."""
    from PIL import Image

    from backend.ai.documents import render_visible_pages

    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    out = tmp_path / "pages"
    out.mkdir()

    with Image.open(render_visible_pages(path, str(out))[0]) as page:
        page.load()
        assert _ink_in_band(page, 725, 690) > 0
        assert _ink_in_band(page, 425, 390) == 0

    # And a PNG has no text layer at all, so there is nothing left to read out
    # of it -- which is the property the fix relies on.


def test_a_raster_upload_is_passed_through_untouched(tmp_path):
    """A PNG is already one visible page. Nothing to render, nothing to re-encode."""
    from backend.ai.documents import render_visible_pages

    source = tmp_path / "scan.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n synthetic")
    assert render_visible_pages(str(source), str(tmp_path)) == [str(source)]


def test_an_unreadable_document_is_refused_not_sent_anyway(tmp_path):
    """A fallback to the original file would silently restore the whole bug."""
    from backend.ai.documents import DocumentNormalisationError, render_visible_pages

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a document")
    with pytest.raises(DocumentNormalisationError) as exc:
        render_visible_pages(str(broken), str(tmp_path))
    assert exc.value.code in ("document_unreadable", "document_empty", "page_render_failed")


def test_a_missing_document_is_named(tmp_path):
    from backend.ai.documents import DocumentNormalisationError, render_visible_pages

    with pytest.raises(DocumentNormalisationError) as exc:
        render_visible_pages(str(tmp_path / "nope.pdf"), str(tmp_path))
    assert exc.value.code == "source_missing"


@pytest.mark.asyncio
async def test_the_page_images_are_cleaned_up_after_the_call(tmp_path):
    """A failed provider call must not leave exam pages on disk."""
    from backend.ai.documents import visible_pages

    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    with pytest.raises(RuntimeError):
        async with visible_pages(path) as pages:
            seen = list(pages)
            assert all(os.path.exists(p) for p in seen)
            raise RuntimeError("the provider call failed")
    assert not any(os.path.exists(p) for p in seen), "page images outlived the call"


@pytest.mark.asyncio
async def test_a_raster_source_creates_no_workspace(tmp_path):
    from backend.ai.documents import visible_pages

    source = tmp_path / "scan.jpg"
    source.write_bytes(b"\xff\xd8\xff synthetic")
    async with visible_pages(str(source)) as pages:
        assert list(pages) == [str(source)]
    assert source.exists(), "a raster source must not be deleted with a workspace"


# ---------------------------------------------------------------------------
# what the provider is actually handed
# ---------------------------------------------------------------------------

class _MediaRecorder:
    """Records what each file part WAS at the moment the task ran.

    Reading at call time matters: the page images live only for the duration of
    the call, so asserting after the fact would find nothing there.
    """

    name = "gemini"

    def __init__(self, body=""):
        self.body = body
        self.media = []

    async def run_text_task(self, request, settings, *, timeout_seconds=None):
        from backend.ai.contracts import ProviderResponse

        for path in request.file_paths:
            with open(path, "rb") as handle:
                head = handle.read(8)
            self.media.append({"path": path, "head": head,
                               "suffix": os.path.splitext(path)[1].lower()})
        return ProviderResponse(
            text=self.body, provider=self.name, model="test-model",
            task=request.task, prompt_version=request.prompt_version,
            attempts=1, duration_ms=0, uploaded_file_count=len(request.file_paths),
            warnings=(),
        )


@pytest.fixture
def media_recorder():
    from backend.ai import providers

    recorder = _MediaRecorder()
    providers.register_provider(recorder.name, recorder)
    yield recorder
    providers.reset_providers()


@pytest.mark.asyncio
@pytest.mark.parametrize("service_name", ["extract_question_labels", "extract_document_text"])
async def test_a_document_task_never_receives_the_pdf(tmp_path, media_recorder, service_name):
    """Before the fix this sent `application/pdf` -- the whole file, hidden text and all."""
    from backend.ai import services

    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    await getattr(services, service_name)(path, exam_id=1)

    assert media_recorder.media, "the task sent no document at all"
    for item in media_recorder.media:
        assert item["suffix"] == ".png", f"a {item['suffix']} reached the provider"
        assert item["head"] == b"\x89PNG\r\n\x1a\n"
        assert item["path"] != path, "the uploaded PDF itself was sent"


@pytest.mark.asyncio
async def test_the_hidden_question_is_not_in_what_the_provider_receives(tmp_path, media_recorder):
    """The bytes the provider gets must not contain a readable question 33."""
    from backend.ai import services

    path = paper_with_hidden_question(tmp_path / "paper.pdf")
    await services.extract_question_labels(path, exam_id=1)

    sent_pdf_text = pdfium.PdfDocument(path)[0].get_textpage().get_text_bounded()
    assert "33. Hidden question" in sent_pdf_text, "precondition: the PDF still hides it"

    for item in media_recorder.media:
        assert item["suffix"] != ".pdf"
    # The provider was handed rendered pages; a PNG carries no text objects, so
    # the only route to question 33 -- reading the file's text layer -- is gone.


# ---------------------------------------------------------------------------
# parsing the model's answer
# ---------------------------------------------------------------------------

def test_the_label_parser_reads_numbers_labels_and_marks():
    from backend.routers.geminiAPI import _parse_question_labels

    parsed = list(_parse_question_labels(
        "31 - Max Marks - 3\n31.a\n31.a.i\n31.b\n32 - Max Marks - 5\n32.a\n"
    ))
    assert parsed == [
        (31, ["31.a", "31.a.i", "31.b"], 3),
        (32, ["32.a"], 5),
    ]


def test_the_label_parser_is_ordered_and_deduplicated():
    from backend.routers.geminiAPI import _parse_question_labels

    parsed = list(_parse_question_labels("32 - Max Marks - 5\n32.a\n32.a\n31 - Max Marks - 3\n"))
    assert [q for q, _, _ in parsed] == [31, 32], "questions must come out in order"
    assert parsed[1][1] == ["32.a"], "a repeated label must not be duplicated"


# ---------------------------------------------------------------------------
# replacing an exam's question structure
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def extract_client(session_factory, world, monkeypatch, tmp_path):
    """An ASGI client over the real extraction router, with the model stubbed.

    The stub is on the SERVICE, not the provider: this half of the file is
    about what the route does with an answer, and the other half already
    proves what the provider is handed.
    """
    from backend.database import get_db
    from backend.routers import geminiAPI
    from backend.utils.security import get_current_user_required

    answers = []

    async def _fake_extract(document_path, *, exam_id=None):
        return answers.pop(0) if answers else ""

    monkeypatch.setattr(geminiAPI.ai_services, "extract_question_labels", _fake_extract)
    # Uploads land in a throwaway directory, not in the repository's ./uploads.
    monkeypatch.setattr(geminiAPI, "UPLOAD_DIRECTORY", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()

    app = FastAPI()
    app.include_router(geminiAPI.router)

    async def _override_db():
        async with session_factory() as s:
            yield s

    async def _override_user(request: Request):
        raw = request.headers.get("X-Test-User")
        if not raw:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Not authenticated")
        async with session_factory() as s:
            found = (await s.execute(select(User).where(User.id == int(raw)))).scalars().first()
        if not found:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Not authenticated")
        return found

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = _override_user

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        c.answers = answers
        yield c


async def _process(client, exam_id, user, model_answer, *, files=None):
    """Drive one question-paper processing run with a stubbed model answer."""
    client.answers.clear()
    client.answers.extend(files or [model_answer])
    upload = [("files", (f"paper-{i}.pdf", b"%PDF-1.4 stub", "application/pdf"))
              for i in range(len(client.answers))]
    return await client.post(
        "/extract-question-labels",
        data={"exam_id": str(exam_id)},
        files=upload,
        headers=as_user(user),
    )


async def _rows(db, exam_id):
    """Read the exam's questions FRESH.

    `expunge_all` first: the route writes through its own session, and SQLite
    reuses primary keys after a delete, so a cached instance could otherwise
    stand in for a row that replaced it.
    """
    db.expunge_all()
    return (await db.execute(
        select(Question).where(Question.exam_id == exam_id).order_by(Question.question_number)
    )).scalars().all()


async def _numbers(db, exam_id):
    return [row.question_number for row in await _rows(db, exam_id)]


@pytest.mark.asyncio
async def test_reprocessing_replaces_the_structure_it_does_not_append(extract_client, db, world):
    """31,32,33 then 31,32 must leave 31,32 -- the exact case from the report."""
    exam = world["exam_b"]
    prof = world["other_prof"]

    await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n32 - Max Marks - 3\n33 - Max Marks - 3\n")
    assert await _numbers(db, exam.id) == [31, 32, 33]

    res = await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n32 - Max Marks - 3\n")
    assert res.status_code == 200, res.text
    assert await _numbers(db, exam.id) == [31, 32], "question 33 survived the replacement"


@pytest.mark.asyncio
async def test_stale_subparts_do_not_survive_a_replacement(extract_client, db, world):
    exam = world["exam_b"]
    prof = world["other_prof"]

    await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n31.a\n31.b\n31.c\n")
    rows = await _rows(db, exam.id)
    assert [r.question_number for r in rows] == [31], "the old structure was not replaced"
    assert json.loads(rows[0].part_labels) == ["31.a", "31.b", "31.c"]

    await _process(extract_client, exam.id, prof, "31 - Max Marks - 2\n31.a\n")
    rows = await _rows(db, exam.id)
    assert len(rows) == 1, "replacement left more than one row for question 31"
    assert json.loads(rows[0].part_labels) == ["31.a"], "31.b and 31.c survived"
    assert rows[0].max_marks == 2, "the marks were not replaced either"


@pytest.mark.asyncio
async def test_processing_the_same_paper_twice_is_idempotent(extract_client, db, world):
    """No duplicate rows: there is no unique constraint to catch them."""
    exam = world["exam_b"]
    prof = world["other_prof"]
    answer = "31 - Max Marks - 3\n31.a\n32 - Max Marks - 4\n"

    await _process(extract_client, exam.id, prof, answer)
    first = await _numbers(db, exam.id)
    await _process(extract_client, exam.id, prof, answer)
    assert await _numbers(db, exam.id) == first == [31, 32]


@pytest.mark.asyncio
async def test_two_files_in_one_upload_do_not_append_to_each_other(extract_client, db, world):
    """Each file used to replace nothing and add its own rows."""
    exam = world["exam_b"]
    prof = world["other_prof"]

    await _process(extract_client, exam.id, prof, None,
                   files=["31 - Max Marks - 3\n", "32 - Max Marks - 4\n"])
    assert await _numbers(db, exam.id) == [31, 32], "both files must land in one structure"

    await _process(extract_client, exam.id, prof, None, files=["31 - Max Marks - 3\n"])
    assert await _numbers(db, exam.id) == [31], "the second file's question survived"


@pytest.mark.asyncio
async def test_replacement_is_refused_once_a_student_has_responses(extract_client, db, world):
    """Deleting the questions would cascade the responses and their marks away."""
    exam = world["exam_b"]
    prof = world["other_prof"]
    student = world["student_a"]

    await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n32 - Max Marks - 3\n")
    question = next(r for r in await _rows(db, exam.id) if r.question_number == 31)
    db.add(QuestionResponse(question_id=question.id, student_id=student.id,
                            answer_text="an answer", marks_obtained=2))
    await db.commit()

    res = await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n")
    assert res.status_code == 409, res.text
    assert "student responses" in res.json()["detail"]

    assert await _numbers(db, exam.id) == [31, 32], "the refusal still changed the structure"
    kept = (await db.execute(
        select(QuestionResponse)
        .join(Question, QuestionResponse.question_id == Question.id)
        .where(Question.exam_id == exam.id)
    )).scalars().all()
    assert [r.marks_obtained for r in kept] == [2], "a refused replacement lost a mark"


@pytest.mark.asyncio
async def test_a_failed_extraction_writes_nothing(extract_client, db, world, monkeypatch):
    """The old structure and the new one are never both absent."""
    from backend.routers import geminiAPI

    exam = world["exam_b"]
    prof = world["other_prof"]
    await _process(extract_client, exam.id, prof, "31 - Max Marks - 3\n32 - Max Marks - 3\n")

    async def _boom(document_path, *, exam_id=None):
        raise RuntimeError("the provider call failed")

    monkeypatch.setattr(geminiAPI.ai_services, "extract_question_labels", _boom)
    res = await _process(extract_client, exam.id, prof, "irrelevant")
    assert res.status_code >= 500

    assert await _numbers(db, exam.id) == [31, 32], "a failed run emptied the exam"


@pytest.mark.asyncio
async def test_only_a_manager_can_replace_the_structure(extract_client, db, world):
    exam = world["exam_b"]
    for actor in ("student_a", "owner_prof", "outsider"):
        res = await _process(extract_client, exam.id, world[actor], "31 - Max Marks - 3\n")
        assert res.status_code in (403, 404), f"{actor} got {res.status_code}"
    # The structure the world fixture gave exam B, untouched.
    assert await _numbers(db, exam.id) == [1], "an unauthorised caller changed the structure"


# ---------------------------------------------------------------------------
# the marking scheme must not invent questions
# ---------------------------------------------------------------------------

def test_only_two_places_in_the_codebase_create_a_question():
    """A structural guard: a third one is where the next phantom question comes from."""
    import ast
    import pathlib

    creators = []
    for path in pathlib.Path(REPO_ROOT / "backend").rglob("*.py"):
        if "old" in path.parts or "tests" in path.parts or "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "Question":
                creators.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert len(creators) == 2, f"a new Question constructor appeared: {creators}"
    assert any("routers/exams.py" in c or "routers\\exams.py" in c for c in creators)
    assert any("routers/geminiAPI.py" in c or "routers\\geminiAPI.py" in c for c in creators)


@pytest.mark.asyncio
async def test_marking_scheme_processing_cannot_add_a_question(extract_client, db, world,
                                                               fake_provider):
    """Even when the model answers about a question the exam does not have."""
    exam = world["exam_a"]
    before = await _numbers(db, exam.id)
    assert before == [1]

    question = world["q1"]
    question.ms_text_images = json.dumps([str(REPO_ROOT / "uploads" / "test_marking_scheme.pdf")])
    await db.commit()

    fake_provider(body="Key: 99\nA marking scheme for a question this exam does not have\n")

    res = await extract_client.post(
        f"/{exam.id}/process-text-images/marking_scheme",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200, res.text
    assert await _numbers(db, exam.id) == before, (
        "marking-scheme processing created a question"
    )
