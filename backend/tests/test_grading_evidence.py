"""Regression tests for reference-vs-student separation in diagram grading.

THE BUG THESE PROTECT AGAINST
-----------------------------
`grade_question_with_diagram` used to build a single `uploaded_files` list that
contained only the STUDENT's table and diagram images, and then spliced that
same list into both the marking-scheme slot and the student-answer slot of the
prompt. The marking-scheme images were loaded from the database and never
uploaded. The grader was shown the student's own drawing as the reference, so
marks came from comparing a diagram with itself.

Two layers are tested:

  * `backend.grading.evidence` -- pure, provider-free, no mocking needed.
  * the request assembly inside the router -- with the Gemini uploader stubbed,
    so no network call and no API quota is used.

`test_student_files_never_reach_the_reference_slot` is the exact historical
regression: it fails against the old control flow and passes against the new.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.grading.evidence import (
    GradingEvidence,
    ImageSet,
    build_grading_evidence,
    parse_image_paths,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_question(*, text="Q", max_marks=10, ms_text=None, ms_table=None,
                  ms_diagram=None, question_number=1):
    """A stand-in for the Question ORM row, holding only what grading reads."""
    return SimpleNamespace(
        question_number=question_number,
        text=text,
        max_marks=max_marks,
        ms_text_images=ms_text,
        ms_table_images=ms_table,
        ms_diagram_images=ms_diagram,
    )


def make_response(*, answer_text="student wrote this", ans_text=None,
                  ans_table=None, ans_diagram=None):
    return SimpleNamespace(
        answer_text=answer_text,
        ans_text_images=ans_text,
        ans_table_images=ans_table,
        ans_diagram_images=ans_diagram,
    )


def js(*paths):
    return json.dumps(list(paths))


# ---------------------------------------------------------------------------
# parse_image_paths  (Phase E: None / [] / malformed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, []),
    ("", []),
    ("[]", []),
    ("null", []),
    ("not json at all", []),
    ('{"a": 1}', []),          # valid JSON, wrong shape
    ('["a.png"]', ["a.png"]),
    ('["a.png", "b.png"]', ["a.png", "b.png"]),
    ('["a.png", "", "  "]', ["a.png"]),
    ('["a.png", 5, null]', ["a.png"]),
    (["c.png"], ["c.png"]),
])
def test_parse_image_paths_normalises(raw, expected):
    assert parse_image_paths(raw) == expected


def test_parse_image_paths_never_raises_on_none():
    """The old code called len() on this value when it was None."""
    assert parse_image_paths(None) == []
    assert len(parse_image_paths(None)) == 0


# ---------------------------------------------------------------------------
# Test 1 -- different reference and student images
# ---------------------------------------------------------------------------

def test_reference_and_student_images_stay_separate():
    ev = build_grading_evidence(
        question=make_question(ms_diagram=js("ms_A.png")),
        question_response=make_response(ans_diagram=js("ans_B.png")),
    )
    assert ev.reference_images.diagram == ["ms_A.png"]
    assert ev.student_images.diagram == ["ans_B.png"]
    # the point of the whole phase:
    assert "ans_B.png" not in ev.reference_images.all_paths
    assert "ms_A.png" not in ev.student_images.all_paths


# ---------------------------------------------------------------------------
# Test 2 -- multiple images on both sides
# ---------------------------------------------------------------------------

def test_multiple_images_stay_on_the_correct_side():
    ev = build_grading_evidence(
        question=make_question(
            ms_diagram=js("ms_diagram_1.png", "ms_diagram_2.png"),
            ms_table=js("ms_table_1.png"),
        ),
        question_response=make_response(
            ans_diagram=js("ans_diagram_1.png"),
            ans_table=js("ans_table_1.png"),
        ),
    )
    assert set(ev.reference_images.all_paths) == {
        "ms_diagram_1.png", "ms_diagram_2.png", "ms_table_1.png"
    }
    assert set(ev.student_images.all_paths) == {"ans_diagram_1.png", "ans_table_1.png"}
    assert not set(ev.reference_images.all_paths) & set(ev.student_images.all_paths)


def test_image_order_is_stable():
    """Ordering must be deterministic: text, then tables, then diagrams."""
    ev = build_grading_evidence(
        question=make_question(
            ms_text=js("t1.png"), ms_table=js("tab1.png", "tab2.png"),
            ms_diagram=js("d1.png"),
        ),
        question_response=make_response(),
    )
    assert ev.reference_images.all_paths == ["t1.png", "tab1.png", "tab2.png", "d1.png"]


# ---------------------------------------------------------------------------
# Test 3 -- student diagram, no reference diagram
# ---------------------------------------------------------------------------

def test_student_diagram_without_any_reference_image():
    ev = build_grading_evidence(
        question=make_question(),                      # no ms images at all
        question_response=make_response(ans_diagram=js("ans_only.png")),
    )
    assert ev.student_images.diagram == ["ans_only.png"]
    assert ev.reference_images.all_paths == [], (
        "a missing marking-scheme image must stay missing, never be substituted"
    )
    assert ev.reference_images.has_any is False


# ---------------------------------------------------------------------------
# Test 4 -- reference diagram, no student diagram
# ---------------------------------------------------------------------------

def test_reference_diagram_without_student_diagram():
    ev = build_grading_evidence(
        question=make_question(ms_diagram=js("ms_only.png")),
        question_response=make_response(answer_text="text only"),
    )
    assert ev.reference_images.diagram == ["ms_only.png"]
    assert ev.student_images.all_paths == []
    assert ev.has_student_evidence is True          # text still counts


# ---------------------------------------------------------------------------
# Test 5 -- None and [] across every field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("empty", [None, "[]", ""])
def test_all_image_fields_empty_is_safe(empty):
    ev = build_grading_evidence(
        question=make_question(ms_text=empty, ms_table=empty, ms_diagram=empty),
        question_response=make_response(answer_text=None, ans_text=empty,
                                        ans_table=empty, ans_diagram=empty),
    )
    assert ev.reference_images.all_paths == []
    assert ev.student_images.all_paths == []
    assert ev.has_student_evidence is False
    assert ev.reference_images.descriptor() == ""


def test_missing_question_response_is_safe():
    """A student with no recorded response must not crash evidence assembly."""
    ev = build_grading_evidence(
        question=make_question(ms_diagram=js("ms.png")),
        question_response=None,
    )
    assert ev.student_images.all_paths == []
    assert ev.student_answer_text is None
    assert ev.has_student_evidence is False
    assert ev.reference_images.diagram == ["ms.png"]


# ---------------------------------------------------------------------------
# descriptors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [
    ({}, ""),
    ({"diagram": ["d"]}, "diagrams"),
    ({"table": ["t"]}, "tables"),
    ({"diagram": ["d"], "table": ["t"]}, "diagrams and tables"),
    ({"text": ["x"]}, "images"),
])
def test_descriptor_matches_contents(kwargs, expected):
    assert ImageSet(**kwargs).descriptor() == expected


def test_empty_imageset_reports_nothing_present():
    """The old helper initialised image_present=True before deciding."""
    assert ImageSet().has_any is False
    assert ImageSet().descriptor() == ""


# ---------------------------------------------------------------------------
# student text images are represented by extracted text, not re-sent
# ---------------------------------------------------------------------------

def test_student_text_images_are_not_attached_as_images():
    """They already became answer_text upstream; attaching them would duplicate."""
    ev = build_grading_evidence(
        question=make_question(),
        question_response=make_response(ans_text=js("ans_text_1.png")),
    )
    assert ev.student_images.text == []
    assert "ans_text_1.png" not in ev.student_images.all_paths


def test_reference_text_images_are_attached():
    """No working extraction path exists for these, so they must be sent."""
    ev = build_grading_evidence(
        question=make_question(ms_text=js("ms_text_1.png")),
        question_response=make_response(),
    )
    assert ev.reference_images.text == ["ms_text_1.png"]


# ---------------------------------------------------------------------------
# Test 6 -- exact historical regression, at the request-assembly layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_student_files_never_reach_the_reference_slot(monkeypatch, tmp_path):
    """End-to-end over the real handler with the uploader stubbed.

    Asserts on the actual `prompt_content` handed to the model: the reference
    slot must contain the marking-scheme handle and must NOT contain the
    student's. Against the previous implementation the reference slot held the
    student's file, so this fails there and passes here.
    """
    from backend.routers import geminiAPI

    ms_path = tmp_path / "ms_A.png"
    ans_path = tmp_path / "ans_B.png"
    ms_path.write_bytes(b"ms")
    ans_path.write_bytes(b"ans")

    uploaded = []

    class FakeHandle:
        def __init__(self, path):
            self.name = f"files/{path.split('/')[-1].split(chr(92))[-1]}"
            self.path = path

        def __repr__(self):
            return f"<FakeHandle {self.name}>"

    def fake_upload(path=None, display_name=None, **kw):
        uploaded.append(path)
        return FakeHandle(path)

    captured = {}

    class FakeModel:
        def generate_content(self, prompt_content):
            captured["prompt_content"] = prompt_content
            return SimpleNamespace(text="Grade: 5\nReason: ok")

    monkeypatch.setattr(geminiAPI.genai, "upload_file", fake_upload, raising=False)
    monkeypatch.setattr(geminiAPI, "get_model", lambda: FakeModel())

    question = make_question(ms_diagram=js(str(ms_path)))
    qr = make_response(answer_text="my answer", ans_diagram=js(str(ans_path)))
    qr.marks_obtained = None
    qr.reasoning = None

    class FakeResult:
        def __init__(self, obj):
            self._obj = obj

        def scalars(self):
            return self

        def first(self):
            return self._obj

    sequence = [qr, question, qr]

    class FakeDB:
        async def execute(self, *a, **kw):
            return FakeResult(sequence.pop(0))

        async def commit(self):
            return None

    await geminiAPI.grade_question_with_diagram(
        {
            "ideal_answer": None,
            "marking_scheme": "the reference scheme text",
            "exam_id": 1,
            "student_id": 2,
            "question_id": 3,
        },
        FakeDB(),
        None,          # current_user None -> authorization handled by the caller
    )

    parts = captured["prompt_content"]
    handles = [p for p in parts if isinstance(p, FakeHandle)]
    assert len(handles) == 2, f"expected one reference and one student file, got {handles}"

    ref_marker = parts.index("[REFERENCE / MARKING SCHEME IMAGES]")
    stu_marker = parts.index("[STUDENT ANSWER IMAGES]")

    ref_handle = parts[ref_marker + 1]
    stu_handle = parts[stu_marker + 1]

    assert ref_handle.path == str(ms_path), "reference slot must hold the marking-scheme image"
    assert stu_handle.path == str(ans_path), "student slot must hold the student image"
    assert ref_handle.path != str(ans_path), (
        "REGRESSION: the student's image reached the marking-scheme slot"
    )
    assert ref_marker < stu_marker, "reference material must precede student evidence"
    assert sorted(uploaded) == sorted([str(ms_path), str(ans_path)]), (
        "each image must be uploaded exactly once"
    )


@pytest.mark.asyncio
async def test_missing_reference_leaves_reference_slot_absent(monkeypatch, tmp_path):
    """With no marking-scheme image, no reference image section may appear."""
    from backend.routers import geminiAPI

    ans_path = tmp_path / "ans_only.png"
    ans_path.write_bytes(b"ans")

    class FakeHandle:
        def __init__(self, path):
            self.path = path

    monkeypatch.setattr(geminiAPI.genai, "upload_file",
                        lambda path=None, display_name=None, **kw: FakeHandle(path),
                        raising=False)

    captured = {}

    class FakeModel:
        def generate_content(self, prompt_content):
            captured["prompt_content"] = prompt_content
            return SimpleNamespace(text="Grade: 1\nReason: ok")

    monkeypatch.setattr(geminiAPI, "get_model", lambda: FakeModel())

    question = make_question()                      # no reference images
    qr = make_response(answer_text="a", ans_diagram=js(str(ans_path)))
    qr.marks_obtained = None
    qr.reasoning = None

    class FakeResult:
        def __init__(self, obj):
            self._obj = obj

        def scalars(self):
            return self

        def first(self):
            return self._obj

    sequence = [qr, question, qr]

    class FakeDB:
        async def execute(self, *a, **kw):
            return FakeResult(sequence.pop(0))

        async def commit(self):
            return None

    await geminiAPI.grade_question_with_diagram(
        {"ideal_answer": "ideal", "marking_scheme": None,
         "exam_id": 1, "student_id": 2, "question_id": 3},
        FakeDB(),
        None,
    )

    parts = captured["prompt_content"]
    assert "[REFERENCE / MARKING SCHEME IMAGES]" not in parts, (
        "no reference image section may be emitted when there is no reference image"
    )
    handles = [p for p in parts if isinstance(p, FakeHandle)]
    assert len(handles) == 1 and handles[0].path == str(ans_path)
