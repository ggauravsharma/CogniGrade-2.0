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
    # one category at a time
    ({"diagram": ["d"]}, "diagrams"),
    ({"table": ["t"]}, "tables"),
    ({"math": ["m"]}, "mathematical working"),
    ({"text": ["x"]}, "text images"),
    # combinations: EVERY present category is named, in attachment order
    ({"table": ["t"], "diagram": ["d"]}, "tables and diagrams"),
    ({"math": ["m"], "diagram": ["d"]}, "mathematical working and diagrams"),
    ({"math": ["m"], "table": ["t"]}, "mathematical working and tables"),
    ({"math": ["m"], "table": ["t"], "diagram": ["d"]},
     "mathematical working, tables and diagrams"),
    ({"text": ["x"], "math": ["m"], "table": ["t"], "diagram": ["d"]},
     "text images, mathematical working, tables and diagrams"),
])
def test_descriptor_matches_contents(kwargs, expected):
    assert ImageSet(**kwargs).descriptor() == expected


@pytest.mark.parametrize("kwargs", [
    {"math": ["m"]},
    {"math": ["m"], "diagram": ["d"]},
    {"math": ["m"], "table": ["t"]},
])
def test_maths_is_never_described_as_a_diagram(kwargs):
    """It used to be bucketed as one, so the prompt asserted something false."""
    described = ImageSet(**kwargs).descriptor()
    assert "mathematical working" in described
    if not kwargs.get("diagram"):
        assert "diagram" not in described


def test_a_category_with_no_files_is_never_mentioned():
    described = ImageSet(table=["t"]).descriptor()
    assert described == "tables"
    for absent in ("diagram", "mathematical", "text"):
        assert absent not in described


def test_the_descriptor_names_every_attached_category():
    """The old implementation returned the FIRST match and hid the rest."""
    from backend.grading.evidence import CATEGORY_LABELS

    full = ImageSet(text=["x"], math=["m"], table=["t"], diagram=["d"])
    described = full.descriptor()
    for label in CATEGORY_LABELS.values():
        assert label in described, f"{label} was attached but not described"


def test_present_categories_follows_attachment_order():
    full = ImageSet(text=["x"], math=["m"], table=["t"], diagram=["d"])
    assert full.present_categories == ("text", "math", "table", "diagram")
    assert full.all_paths == ["x", "m", "t", "d"]


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
async def test_student_files_never_reach_the_reference_slot(fake_provider, tmp_path):
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

    provider = fake_provider(body='{"score": 5, "reason": "ok"}')

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

    from backend.ai.contracts import FilePart, TextPart
    from backend.ai.prompts.grading import (
        REFERENCE_IMAGE_HEADING,
        STUDENT_IMAGE_HEADING,
    )

    parts = provider.last_parts()
    files = [p for p in parts if isinstance(p, FilePart)]
    assert len(files) == 2, f"expected one reference and one student file, got {files}"

    texts = [p.text if isinstance(p, TextPart) else None for p in parts]
    ref_marker = texts.index(REFERENCE_IMAGE_HEADING)
    stu_marker = texts.index(STUDENT_IMAGE_HEADING)

    ref_file = parts[ref_marker + 1]
    stu_file = parts[stu_marker + 1]

    assert ref_file.path == str(ms_path), "reference slot must hold the marking-scheme image"
    assert stu_file.path == str(ans_path), "student slot must hold the student image"
    assert ref_file.path != str(ans_path), (
        "REGRESSION: the student's image reached the marking-scheme slot"
    )
    assert ref_marker < stu_marker, "reference material must precede student evidence"
    assert sorted(provider.last_paths()) == sorted([str(ms_path), str(ans_path)]), (
        "each image must appear exactly once"
    )


@pytest.mark.asyncio
async def test_missing_reference_leaves_reference_slot_absent(fake_provider, tmp_path):
    """With no marking-scheme image, no reference image section may appear."""
    from backend.routers import geminiAPI

    ans_path = tmp_path / "ans_only.png"
    ans_path.write_bytes(b"ans")

    provider = fake_provider(body='{"score": 1, "reason": "ok"}')

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

    from backend.ai.prompts.grading import REFERENCE_IMAGE_HEADING

    assert REFERENCE_IMAGE_HEADING not in provider.last_texts(), (
        "no reference image section may be emitted when there is no reference image"
    )
    paths = provider.last_paths()
    assert paths == [str(ans_path)]


# ---------------------------------------------------------------------------
# what the descriptor actually says in the assembled prompt
# ---------------------------------------------------------------------------

def _prompt_text(evidence, *, marking_scheme="the scheme", ideal_answer=None):
    """The concatenated text of one assembled grading prompt."""
    from backend.ai.contracts import TextPart
    from backend.routers.geminiAPI import _build_diagram_prompt_parts

    parts = _build_diagram_prompt_parts(
        make_question(), evidence,
        marking_scheme=marking_scheme, ideal_answer=ideal_answer,
    )
    return "\n".join(p.text for p in parts if isinstance(p, TextPart))


def _evidence_with(student_images, *, answer_text="the recognised answer"):
    return GradingEvidence(
        question_text="q", max_marks=5,
        marking_scheme_text="the scheme",
        student_answer_text=answer_text,
        student_images=student_images,
    )


def test_the_prompt_never_calls_attached_mathematics_a_diagram():
    """The mapping used to file maths under `diagram`, so the prompt said so."""
    text = _prompt_text(_evidence_with(ImageSet(math=["/tmp/m.png"])))
    assert "mathematical working" in text
    assert "diagram" not in text


def test_the_prompt_names_every_attached_student_category():
    text = _prompt_text(_evidence_with(
        ImageSet(math=["/tmp/m.png"], table=["/tmp/t.png"], diagram=["/tmp/d.png"])
    ))
    assert "mathematical working, tables and diagrams" in text


def test_the_prompt_omits_categories_with_no_files():
    text = _prompt_text(_evidence_with(ImageSet(table=["/tmp/t.png"])))
    assert "the attached tables" in text
    assert "mathematical working" not in text
    assert "diagrams" not in text


def test_the_prompt_describes_the_two_sides_separately():
    """A student category must never be announced in the marking-scheme slot."""
    evidence = GradingEvidence(
        question_text="q", max_marks=5,
        marking_scheme_text="the scheme",
        reference_images=ImageSet(diagram=["/tmp/ms.png"]),
        student_answer_text="answer",
        student_images=ImageSet(math=["/tmp/m.png"]),
    )
    text = _prompt_text(evidence)
    scheme_half, student_half = text.split("Grade the following student answer", 1)
    assert "diagrams" in scheme_half and "mathematical working" not in scheme_half
    assert "mathematical working" in student_half


def test_a_question_with_no_student_images_says_nothing_about_attachments():
    text = _prompt_text(_evidence_with(ImageSet()))
    for label in ("the attached", "mathematical working", "tables", "diagrams"):
        assert label not in text.split("Grade the following student answer", 1)[1]
