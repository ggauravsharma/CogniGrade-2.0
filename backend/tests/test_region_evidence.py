"""Structured regions as grading evidence.

The claim under test: when a teacher has ACCEPTED regions on a page, grading
sees crops cut from the original page at those coordinates -- and when they
have not, grading sees exactly what it saw before this existed.

Everything that must not happen is asserted too: a model's unreviewed proposal
must not move a mark, the teacher's red pen must not become the student's
answer, a student region must never reach the reference slot, and a failure to
prepare evidence must never look like a zero.

Synthetic pages with known geometry, so the pixel assertions are exact. NO LIVE
PROVIDER -- zero API quota.
"""

import ast
import json
import os
import pathlib

import pytest
from PIL import Image
from sqlalchemy import select

from backend.grading.evidence import ImageSet
from backend.grading.region_evidence import (
    build_evidence,
    load_answer_script,
    load_regions_for_question,
)
from backend.models.files import AnswerScript
from backend.models.tables import DocumentRegion, QuestionResponse
from backend.regions.cropping import (
    PDF_RENDER_SCALE,
    CropWorkspace,
    PageRenderer,
    RegionEvidenceError,
    crop_region,
)
from backend.regions.evidence import (
    NON_ATTACHED_STUDENT_TYPES,
    REGION_TYPE_TO_BUCKET,
    EvidenceSource,
    attachable_regions,
    build_region_image_set,
    merge_student_evidence,
    select_gradeable_regions,
)
from backend.regions.schema import GeometryKind, RegionSource, RegionStatus, RegionType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# fixtures: a synthetic page with known coloured quadrants
# ---------------------------------------------------------------------------

@pytest.fixture
def page_image(tmp_path):
    """A 400x200 page: left half red, right half blue, with a green square.

    Known geometry means a crop assertion can be exact rather than
    approximate: the right half must be entirely blue, and so on.
    """
    image = Image.new("RGB", (400, 200), (255, 0, 0))
    for x in range(200, 400):
        for y in range(200):
            image.putpixel((x, y), (0, 0, 255))
    for x in range(40, 80):
        for y in range(40, 80):
            image.putpixel((x, y), (0, 255, 0))
    path = tmp_path / "page.png"
    image.save(path)
    return str(path)


class _Region:
    """A duck-typed region row, so the pure helpers need no ORM."""

    def __init__(self, id, page_index=0, region_type=RegionType.HANDWRITTEN_TEXT,
                 geometry_kind=GeometryKind.RECT, geometry=None, question_id=1,
                 status=RegionStatus.ACCEPTED, reading_order=0):
        self.id = id
        self.page_index = page_index
        self.region_type = region_type
        self.geometry_kind = geometry_kind
        self.geometry = geometry if geometry is not None else {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}
        self.question_id = question_id
        self.status = status
        self.reading_order = reading_order


# ---------------------------------------------------------------------------
# provider neutrality
# ---------------------------------------------------------------------------

def test_the_cropping_and_evidence_modules_are_provider_neutral():
    banned = ("google", "genai", "gemini", "openai", "fastapi", "sqlalchemy")
    for name in ("cropping.py", "evidence.py"):
        path = REPO_ROOT / "backend" / "regions" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        for module in modules:
            for token in banned:
                assert token not in module.lower(), f"{name} imports {module}"


# ---------------------------------------------------------------------------
# geometry conversion and cropping
# ---------------------------------------------------------------------------

def test_a_rectangle_crop_lands_on_exactly_the_right_pixels(page_image):
    """Normalised 0..1 -> pixels, independent of any viewer scale."""
    renderer = PageRenderer(page_image)
    crop = crop_region(
        renderer, page_index=0, geometry_kind=GeometryKind.RECT,
        geometry={"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0},
    )
    assert crop.size == (200, 200)
    colours = crop.getcolors()
    assert colours == [(200 * 200, (0, 0, 255))], "the right half must be pure blue"


def test_a_crop_of_the_left_half_is_red_with_the_green_square(page_image):
    renderer = PageRenderer(page_image)
    crop = crop_region(
        renderer, page_index=0, geometry_kind=GeometryKind.RECT,
        geometry={"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
    )
    assert crop.size == (200, 200)
    present = {colour for _count, colour in crop.getcolors()}
    assert present == {(255, 0, 0), (0, 255, 0)}


def test_a_small_offset_crop_is_positioned_exactly(page_image):
    """The green square sits at pixels 40..80 of a 400x200 page."""
    renderer = PageRenderer(page_image)
    crop = crop_region(
        renderer, page_index=0, geometry_kind=GeometryKind.RECT,
        geometry={"x": 0.1, "y": 0.2, "w": 0.1, "h": 0.2},
    )
    assert crop.size == (40, 40)
    assert crop.getcolors() == [(40 * 40, (0, 255, 0))]


def test_polygon_cropping_masks_outside_the_shape(page_image):
    """Bounding box, everything outside the polygon painted white."""
    renderer = PageRenderer(page_image)
    crop = crop_region(
        renderer, page_index=0, geometry_kind=GeometryKind.POLYGON,
        geometry={"points": [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0]]},
    )
    assert crop.size == (200, 200)
    present = {colour for _count, colour in crop.getcolors()}
    assert (255, 255, 255) in present, "outside the polygon must be masked"
    assert (0, 0, 255) in present, "inside the polygon must keep the page content"
    # The top-left corner of the bounding box is outside the triangle.
    assert crop.getpixel((2, 190)) == (255, 255, 255)
    # A point well inside the triangle keeps the page colour.
    assert crop.getpixel((190, 100)) == (0, 0, 255)


def test_the_same_page_is_rendered_once_for_many_regions(page_image):
    """Page-level cache, local to one evidence build."""
    renderer = PageRenderer(page_image)
    for _ in range(4):
        crop_region(renderer, page_index=0, geometry_kind=GeometryKind.RECT,
                    geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2})
    assert renderer.render_count == 1


def test_a_missing_source_is_an_explicit_preparation_failure(tmp_path):
    renderer = PageRenderer(str(tmp_path / "nope.png"))
    with pytest.raises(RegionEvidenceError) as exc:
        renderer.page(0)
    assert exc.value.code == "source_missing"


def test_an_out_of_range_page_is_refused(page_image):
    renderer = PageRenderer(page_image)
    with pytest.raises(RegionEvidenceError) as exc:
        renderer.page(3)
    assert exc.value.code == "page_out_of_range"


def test_a_negative_page_is_refused(page_image):
    with pytest.raises(RegionEvidenceError) as exc:
        PageRenderer(page_image).page(-1)
    assert exc.value.code == "page_out_of_range"


def test_an_unreadable_file_is_an_explicit_failure(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not a png")
    with pytest.raises(RegionEvidenceError) as exc:
        PageRenderer(str(broken)).page(0)
    assert exc.value.code == "page_render_failed"


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_page(tmp_path, page_image):
    pdfium = pytest.importorskip("pypdfium2")
    path = tmp_path / "doc.pdf"
    Image.open(page_image).convert("RGB").save(path, "PDF", resolution=72.0)
    return str(path)


def test_a_pdf_page_renders_and_crops(pdf_page):
    renderer = PageRenderer(pdf_page)
    page = renderer.page(0)
    assert page.size[0] > 0 and page.size[1] > 0
    crop = crop_region(renderer, page_index=0, geometry_kind=GeometryKind.RECT,
                       geometry={"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0})
    # Right half of the synthetic page is blue; allow for PDF colour rounding.
    dominant = max(crop.getcolors(maxcolors=1 << 20), key=lambda c: c[0])[1]
    assert dominant[2] > dominant[0], f"expected a blue-dominant crop, got {dominant}"


def test_a_pdf_page_beyond_the_document_is_refused(pdf_page):
    with pytest.raises(RegionEvidenceError) as exc:
        PageRenderer(pdf_page).page(5)
    assert exc.value.code == "page_out_of_range"


def test_the_pdf_render_scale_is_pinned():
    """Crops must be reproducible; a floating render scale would not be."""
    assert PDF_RENDER_SCALE == 2.0


# ---------------------------------------------------------------------------
# workspace lifecycle
# ---------------------------------------------------------------------------

def test_crops_are_deleted_when_the_workspace_closes(page_image):
    renderer = PageRenderer(page_image)
    with CropWorkspace() as workspace:
        crop = crop_region(renderer, page_index=0, geometry_kind=GeometryKind.RECT,
                           geometry={"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0})
        path = workspace.write(crop, name="r1")
        assert os.path.exists(path)
        directory = workspace.directory
    assert not os.path.exists(path), "a generated crop outlived its workspace"
    assert not os.path.isdir(directory)


def test_crops_are_deleted_even_when_the_body_raises(page_image):
    renderer = PageRenderer(page_image)
    captured = {}
    with pytest.raises(RuntimeError):
        with CropWorkspace() as workspace:
            crop = crop_region(renderer, page_index=0, geometry_kind=GeometryKind.RECT,
                               geometry={"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0})
            captured["path"] = workspace.write(crop, name="r1")
            raise RuntimeError("grading blew up")
    assert not os.path.exists(captured["path"])


def test_nothing_is_written_into_uploads(page_image):
    """Generated crops must never land in the persistent upload tree."""
    renderer = PageRenderer(page_image)
    with CropWorkspace() as workspace:
        crop = crop_region(renderer, page_index=0, geometry_kind=GeometryKind.RECT,
                           geometry={"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0})
        path = workspace.write(crop, name="r1")
    assert "uploads" not in path.replace("\\", "/").split("/")


# ---------------------------------------------------------------------------
# selection policy
# ---------------------------------------------------------------------------

def test_accepted_and_modified_regions_are_selected():
    regions = [
        _Region(1, status=RegionStatus.ACCEPTED),
        _Region(2, status=RegionStatus.MODIFIED),
    ]
    assert [r.id for r in select_gradeable_regions(regions, question_id=1)] == [1, 2]


@pytest.mark.parametrize("status", [RegionStatus.PROPOSED, RegionStatus.REJECTED])
def test_proposed_and_rejected_regions_never_grade(status):
    """A model's unreviewed guess must not be able to move a mark."""
    assert select_gradeable_regions([_Region(1, status=status)], question_id=1) == []


@pytest.mark.parametrize(
    "region_type",
    [RegionType.TEACHER_MARKING, RegionType.CROSSED_OUT,
     RegionType.PAGE_FURNITURE, RegionType.PRINTED_TEXT, RegionType.OTHER],
)
def test_non_answer_content_never_becomes_student_evidence(region_type):
    regions = [_Region(1, region_type=region_type)]
    assert select_gradeable_regions(regions, question_id=1) == []


@pytest.mark.parametrize(
    "region_type",
    [RegionType.HANDWRITTEN_TEXT, RegionType.MATH, RegionType.DIAGRAM, RegionType.TABLE],
)
def test_student_answer_types_are_selected(region_type):
    regions = [_Region(1, region_type=region_type)]
    assert len(select_gradeable_regions(regions, question_id=1)) == 1


def test_an_unassigned_region_is_never_pulled_into_a_question():
    """Real content, but assigning it here would be inventing a link."""
    assert select_gradeable_regions([_Region(1, question_id=None)], question_id=1) == []


def test_another_questions_regions_are_isolated():
    regions = [_Region(1, question_id=1), _Region(2, question_id=2)]
    assert [r.id for r in select_gradeable_regions(regions, question_id=2)] == [2]


def test_selection_order_is_page_then_reading_order_then_id():
    regions = [
        _Region(9, page_index=1, reading_order=0),
        _Region(3, page_index=0, reading_order=5),
        _Region(7, page_index=0, reading_order=1),
        _Region(5, page_index=0, reading_order=1),
    ]
    ordered = select_gradeable_regions(regions, question_id=1)
    assert [r.id for r in ordered] == [5, 7, 3, 9]


def test_selection_order_does_not_depend_on_input_order():
    a = [_Region(1, reading_order=2), _Region(2, reading_order=1)]
    b = list(reversed(a))
    assert ([r.id for r in select_gradeable_regions(a, question_id=1)]
            == [r.id for r in select_gradeable_regions(b, question_id=1)] == [2, 1])


# ---------------------------------------------------------------------------
# region type -> evidence bucket
# ---------------------------------------------------------------------------

def test_every_student_answer_type_is_either_attached_or_explicitly_not():
    """No student-answer type may fall through unhandled."""
    from backend.regions.schema import STUDENT_ANSWER_TYPES

    for region_type in STUDENT_ANSWER_TYPES:
        attached = region_type in REGION_TYPE_TO_BUCKET
        withheld = region_type in NON_ATTACHED_STUDENT_TYPES
        assert attached != withheld, f"{region_type} is both or neither"


def test_no_non_answer_type_has_a_bucket():
    for region_type in (RegionType.TEACHER_MARKING, RegionType.CROSSED_OUT,
                        RegionType.PAGE_FURNITURE, RegionType.PRINTED_TEXT):
        assert region_type not in REGION_TYPE_TO_BUCKET
        assert region_type not in NON_ATTACHED_STUDENT_TYPES


def test_the_mapping_is_the_documented_one():
    assert REGION_TYPE_TO_BUCKET == {
        RegionType.MATH: "math",
        RegionType.DIAGRAM: "diagram",
        RegionType.TABLE: "table",
    }
    assert NON_ATTACHED_STUDENT_TYPES == (RegionType.HANDWRITTEN_TEXT,)


def test_maths_is_not_bucketed_as_a_diagram():
    """It was, and the prompt then told the grader the derivation was a picture."""
    assert REGION_TYPE_TO_BUCKET[RegionType.MATH] == "math"
    assert REGION_TYPE_TO_BUCKET[RegionType.MATH] != REGION_TYPE_TO_BUCKET[RegionType.DIAGRAM]


def test_regions_land_in_their_mapped_buckets(page_image):
    regions = [
        _Region(1, region_type=RegionType.HANDWRITTEN_TEXT, reading_order=0),
        _Region(2, region_type=RegionType.TABLE, reading_order=1),
        _Region(3, region_type=RegionType.DIAGRAM, reading_order=2),
        _Region(4, region_type=RegionType.MATH, reading_order=3),
    ]
    with CropWorkspace() as workspace:
        result = build_region_image_set(regions, source_path=page_image, workspace=workspace)
        # handwritten text is counted, not attached; maths has its own bucket
        assert result.buckets == {"text": 0, "math": 1, "table": 1, "diagram": 1}
        assert result.not_attached_count == 1
        assert result.source == EvidenceSource.STRUCTURED_REGIONS
        assert result.region_count == 4
        assert len(result.image_set.all_paths) == 3
        assert result.image_set.text == []
        assert result.covered_categories == ("math", "table", "diagram")


def test_a_handwritten_text_region_is_never_rendered(page_image):
    """No crop, so no cost -- and nothing to duplicate answer_text with."""
    regions = [_Region(1, region_type=RegionType.HANDWRITTEN_TEXT)]
    with CropWorkspace() as workspace:
        result = build_region_image_set(regions, source_path=page_image, workspace=workspace)
        assert result.image_set.all_paths == []
        assert result.covered_categories == ()
        assert result.not_attached_count == 1
        assert result.render_count == 0, "a page was rasterised for a crop we discard"
        assert workspace.paths == []


# ---------------------------------------------------------------------------
# per-category composition
# ---------------------------------------------------------------------------

def test_structured_wins_only_the_categories_it_covers():
    legacy = ImageSet(table=["legacy-table.png"], diagram=["legacy-diagram.png"])
    structured = ImageSet(table=["fresh-table.png"])
    merged = merge_student_evidence(
        legacy=legacy, structured=structured, covered=("table",)
    )
    assert merged.table == ["fresh-table.png"]
    assert merged.diagram == ["legacy-diagram.png"], "an unrelated legacy category was erased"


def test_a_covered_category_never_carries_both():
    legacy = ImageSet(diagram=["legacy-diagram.png"])
    structured = ImageSet(diagram=["fresh-diagram.png"])
    merged = merge_student_evidence(
        legacy=legacy, structured=structured, covered=("diagram",)
    )
    assert merged.diagram == ["fresh-diagram.png"]
    assert "legacy-diagram.png" not in merged.all_paths


def test_structured_maths_leaves_legacy_tables_alone():
    legacy = ImageSet(table=["legacy-table.png"])
    structured = ImageSet(math=["fresh-math.png"])
    merged = merge_student_evidence(
        legacy=legacy, structured=structured, covered=("math",)
    )
    assert merged.math == ["fresh-math.png"]
    assert merged.table == ["legacy-table.png"]


def test_covering_nothing_leaves_legacy_untouched():
    legacy = ImageSet(table=["t.png"], diagram=["d.png"])
    merged = merge_student_evidence(legacy=legacy, structured=ImageSet(), covered=())
    assert merged == legacy


def test_attachable_regions_ignores_types_that_attach_nothing():
    regions = [
        _Region(1, region_type=RegionType.HANDWRITTEN_TEXT),
        _Region(2, region_type=RegionType.TABLE),
    ]
    assert [r.id for r in attachable_regions(regions)] == [2]


def test_multi_page_regions_render_each_page_once(tmp_path, page_image):
    """Q5 on pages 0 and 1: both appear, and the cache still works per page."""
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "two.pdf"
    first = Image.open(page_image).convert("RGB")
    second = Image.new("RGB", first.size, (0, 255, 0))
    first.save(pdf, "PDF", resolution=72.0, save_all=True, append_images=[second])

    # Explicitly an attachable type: handwritten text is never rendered, so
    # the stub default would exercise nothing here.
    regions = [
        _Region(1, page_index=0, reading_order=0, region_type=RegionType.DIAGRAM),
        _Region(2, page_index=0, reading_order=1, region_type=RegionType.DIAGRAM),
        _Region(3, page_index=1, reading_order=0, region_type=RegionType.DIAGRAM),
    ]
    renderer = PageRenderer(str(pdf))
    with CropWorkspace() as workspace:
        result = build_region_image_set(
            regions, source_path=str(pdf), workspace=workspace, renderer=renderer
        )
        assert result.region_count == 3
        assert result.page_count == 2
        assert result.render_count == 2, "each page must be rasterised exactly once"
    renderer.close()


# ---------------------------------------------------------------------------
# end to end: precedence, fallback and isolation
# ---------------------------------------------------------------------------

async def _script(db, world, student=None):
    student = student or world["student_a"]
    found = await db.execute(select(AnswerScript).where(
        AnswerScript.exam_id == world["exam_a"].id,
        AnswerScript.student_id == student.id,
    ))
    return found.scalars().first()


async def _add_region(db, world, script, *, question_id, status=RegionStatus.ACCEPTED,
                      region_type=RegionType.DIAGRAM, page_index=0, reading_order=0,
                      geometry=None):
    region = DocumentRegion(
        exam_id=world["exam_a"].id, answer_script_id=script.id,
        page_index=page_index, region_type=region_type,
        geometry_kind=GeometryKind.RECT,
        geometry=json.dumps(geometry or {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}),
        question_id=question_id, reading_order=reading_order,
        status=status, source=RegionSource.MODEL,
    )
    db.add(region)
    await db.commit()
    await db.refresh(region)
    return region


async def _point_script_at(db, script, path):
    script.file_path = path
    await db.commit()


@pytest.mark.asyncio
async def test_no_regions_falls_back_to_legacy_crops(db, world):
    script = await _script(db, world)
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    response.ans_diagram_images = json.dumps(["/tmp/legacy-diagram.png"])
    await db.commit()

    with CropWorkspace() as workspace:
        evidence, result = await build_evidence(
            question=question, question_response=response,
            exam_id=world["exam_a"].id, student_id=student.id, db=db,
            workspace=workspace, marking_scheme="scheme",
        )
    assert result.source == EvidenceSource.LEGACY_CROPS
    assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"]


@pytest.mark.asyncio
async def test_accepted_regions_take_precedence_over_legacy(db, world, page_image):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    response.ans_diagram_images = json.dumps(["/tmp/legacy-diagram.png"])
    await db.commit()
    await _add_region(db, world, script, question_id=question.id)

    with CropWorkspace() as workspace:
        evidence, result = await build_evidence(
            question=question, question_response=response,
            exam_id=world["exam_a"].id, student_id=student.id, db=db,
            workspace=workspace, marking_scheme="scheme",
        )
        assert result.source == EvidenceSource.STRUCTURED_REGIONS
        paths = evidence.student_images.all_paths
        assert len(paths) == 1
        # Structured REPLACES legacy: the stale crop must not also be sent.
        assert "/tmp/legacy-diagram.png" not in paths
        assert all(os.path.exists(p) for p in paths)


@pytest.mark.asyncio
async def test_only_proposals_falls_back_conservatively(db, world, page_image):
    """Unreviewed proposals plus valid legacy crops -> use the legacy crops."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    response.ans_diagram_images = json.dumps(["/tmp/legacy-diagram.png"])
    await db.commit()
    await _add_region(db, world, script, question_id=question.id,
                      status=RegionStatus.PROPOSED)

    with CropWorkspace() as workspace:
        evidence, result = await build_evidence(
            question=question, question_response=response,
            exam_id=world["exam_a"].id, student_id=student.id, db=db,
            workspace=workspace, marking_scheme="scheme",
        )
    assert result.source == EvidenceSource.LEGACY_CROPS
    assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"]


@pytest.mark.asyncio
async def test_teacher_marking_regions_do_not_become_evidence(db, world, page_image):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    await _add_region(db, world, script, question_id=question.id,
                      region_type=RegionType.TEACHER_MARKING)

    with CropWorkspace() as workspace:
        _evidence, result = await build_evidence(
            question=question, question_response=response,
            exam_id=world["exam_a"].id, student_id=student.id, db=db,
            workspace=workspace, marking_scheme="scheme",
        )
    assert result.source == EvidenceSource.LEGACY_CROPS


@pytest.mark.asyncio
async def test_a_missing_source_document_is_a_preparation_failure(db, world):
    """Accepted regions but no renderable page: fail, do NOT silently fall back."""
    script = await _script(db, world)
    await _point_script_at(db, script, "")
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    await _add_region(db, world, script, question_id=question.id)

    with CropWorkspace() as workspace:
        with pytest.raises(RegionEvidenceError) as exc:
            await build_evidence(
                question=question, question_response=response,
                exam_id=world["exam_a"].id, student_id=student.id, db=db,
                workspace=workspace, marking_scheme="scheme",
            )
    assert exc.value.code == "source_missing"


@pytest.mark.asyncio
async def test_the_reference_side_never_receives_student_regions(db, world, page_image):
    """AUDIT C1, restated for structured evidence."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    question, student = world["q1"], world["student_a"]
    question.ms_diagram_images = json.dumps(["/tmp/marking-scheme.png"])
    await db.commit()
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    await _add_region(db, world, script, question_id=question.id)

    with CropWorkspace() as workspace:
        evidence, result = await build_evidence(
            question=question, question_response=response,
            exam_id=world["exam_a"].id, student_id=student.id, db=db,
            workspace=workspace, marking_scheme="scheme",
        )
        assert result.source == EvidenceSource.STRUCTURED_REGIONS
        student_paths = set(evidence.student_images.all_paths)
        reference_paths = set(evidence.reference_images.all_paths)

        assert reference_paths == {"/tmp/marking-scheme.png"}
        assert not (student_paths & reference_paths), (
            "REGRESSION: a student region reached the reference slot"
        )
        assert all(workspace.directory in p for p in student_paths)


@pytest.mark.asyncio
async def test_another_students_regions_cannot_enter_evidence(db, world, page_image):
    """The join constrains the student, so a foreign region is unreachable."""
    mine = await _script(db, world, world["student_a"])
    theirs = await _script(db, world, world["student_b"])
    await _point_script_at(db, mine, page_image)
    await _point_script_at(db, theirs, page_image)
    question = world["q1"]

    await _add_region(db, world, theirs, question_id=question.id)

    regions = await load_regions_for_question(
        exam_id=world["exam_a"].id, student_id=world["student_a"].id,
        question_id=question.id, db=db,
    )
    assert regions == [], "another student's region was visible"


@pytest.mark.asyncio
async def test_another_exams_regions_cannot_enter_evidence(db, world, page_image):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    # A region row that names the OTHER exam, however it got there.
    stray = DocumentRegion(
        exam_id=world["exam_b"].id, answer_script_id=script.id, page_index=0,
        region_type=RegionType.DIAGRAM, geometry_kind=GeometryKind.RECT,
        geometry=json.dumps({"x": 0, "y": 0, "w": 0.5, "h": 1.0}),
        question_id=world["q1"].id, reading_order=0,
        status=RegionStatus.ACCEPTED, source=RegionSource.HUMAN,
    )
    db.add(stray)
    await db.commit()

    regions = await load_regions_for_question(
        exam_id=world["exam_a"].id, student_id=world["student_a"].id,
        question_id=world["q1"].id, db=db,
    )
    assert regions == [], "a region from another exam was visible"


@pytest.mark.asyncio
async def test_the_answer_script_is_resolved_from_the_database_not_a_client(db, world):
    """Paths come from the authoritative row; nothing accepts a client path."""
    script = await load_answer_script(world["exam_a"].id, world["student_a"].id, db)
    assert script is not None
    assert script.student_id == world["student_a"].id
    assert script.exam_id == world["exam_a"].id


# ---------------------------------------------------------------------------
# structured vs legacy: precedence is decided one category at a time
# ---------------------------------------------------------------------------

async def _response_with(db, world, *, table=None, diagram=None, answer_text=None):
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    if table is not None:
        response.ans_table_images = json.dumps(table)
    if diagram is not None:
        response.ans_diagram_images = json.dumps(diagram)
    if answer_text is not None:
        response.answer_text = answer_text
    await db.commit()
    return response


async def _build(db, world, response, workspace):
    return await build_evidence(
        question=world["q1"], question_response=response,
        exam_id=world["exam_a"].id, student_id=world["student_a"].id, db=db,
        workspace=workspace, marking_scheme="scheme",
    )


@pytest.mark.asyncio
async def test_a_structured_table_does_not_erase_a_legacy_diagram(db, world, page_image):
    """The silent evidence-loss bug: one accepted region emptied the whole set."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.TABLE)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.MIXED
        assert result.covered_categories == ("table",)
        assert len(evidence.student_images.table) == 1
        assert all(workspace.directory in p for p in evidence.student_images.table)
        assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"], (
            "REGRESSION: an unrelated legacy category was erased"
        )


@pytest.mark.asyncio
async def test_a_structured_diagram_replaces_the_legacy_diagram(db, world, page_image):
    """Within a category the annotation is authoritative; the crop may be stale."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.DIAGRAM)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.STRUCTURED_REGIONS
        assert len(evidence.student_images.diagram) == 1
        assert "/tmp/legacy-diagram.png" not in evidence.student_images.all_paths, (
            "the same answer was sent twice, once from a stale crop"
        )


@pytest.mark.asyncio
async def test_structured_maths_leaves_a_legacy_table_in_place(db, world, page_image):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, table=["/tmp/legacy-table.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.MATH)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.MIXED
        assert result.covered_categories == ("math",)
        assert len(evidence.student_images.math) == 1
        assert evidence.student_images.table == ["/tmp/legacy-table.png"]
        assert evidence.student_images.diagram == []


@pytest.mark.asyncio
async def test_structured_maths_is_never_filed_as_a_diagram(db, world, page_image):
    """It used to be, and the prompt then described the derivation as a picture."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world)
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.MATH)

    with CropWorkspace() as workspace:
        evidence, _result = await _build(db, world, response, workspace)
        assert evidence.student_images.diagram == []
        assert len(evidence.student_images.math) == 1
        described = evidence.student_images.descriptor()
        assert described == "mathematical working"
        assert "diagram" not in described


@pytest.mark.asyncio
async def test_maths_and_a_legacy_diagram_are_both_described(db, world, page_image):
    """Every attached category reaches the prompt wording, not just the first."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.MATH)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.MIXED
        assert evidence.student_images.descriptor() == "mathematical working and diagrams"


@pytest.mark.asyncio
async def test_no_structured_visual_evidence_leaves_legacy_untouched(db, world, page_image):
    """Accepted handwritten text attaches nothing, so it displaces nothing."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(
        db, world, table=["/tmp/legacy-table.png"], diagram=["/tmp/legacy-diagram.png"],
    )
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.HANDWRITTEN_TEXT)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.LEGACY_CROPS
        assert result.not_attached_count == 1
        assert evidence.student_images.table == ["/tmp/legacy-table.png"]
        assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"]


# ---------------------------------------------------------------------------
# handwritten text: represented, transcribed, never attached twice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_handwritten_text_region_does_not_duplicate_the_answer_text(
    db, world, page_image
):
    """`answer_text` already carries the handwriting; the crop must not follow it."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, answer_text="the recognised answer")
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.HANDWRITTEN_TEXT)

    with CropWorkspace() as workspace:
        evidence, _result = await _build(db, world, response, workspace)
        assert evidence.student_answer_text == "the recognised answer"
        assert evidence.student_images.text == []
        assert evidence.student_images.all_paths == []
        assert workspace.paths == [], "a crop was generated for content already in the prompt"


@pytest.mark.asyncio
async def test_handwritten_text_alongside_a_table_attaches_only_the_table(
    db, world, page_image
):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, answer_text="the recognised answer")
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.HANDWRITTEN_TEXT, reading_order=0)
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.TABLE, reading_order=1)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.region_count == 2
        assert result.not_attached_count == 1
        assert evidence.student_images.text == []
        assert len(evidence.student_images.table) == 1
        assert evidence.student_images.descriptor() == "tables"


@pytest.mark.asyncio
async def test_only_handwritten_text_and_no_source_is_not_a_preparation_failure(
    db, world
):
    """Nothing structured was ever going to be produced, so nothing failed.

    The fail-closed rule still applies wherever a region WOULD have attached an
    image -- see `test_a_missing_source_document_is_a_preparation_failure`.
    """
    script = await _script(db, world)
    await _point_script_at(db, script, "")
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.HANDWRITTEN_TEXT)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
    assert result.source == EvidenceSource.LEGACY_CROPS
    assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"]


@pytest.mark.asyncio
async def test_a_missing_source_still_fails_when_a_region_would_attach(db, world):
    """Fail-closed preserved: a stale legacy crop is not a substitute (audit C6)."""
    script = await _script(db, world)
    await _point_script_at(db, script, "")
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.TABLE)

    with CropWorkspace() as workspace:
        with pytest.raises(RegionEvidenceError) as exc:
            await _build(db, world, response, workspace)
    assert exc.value.code == "source_missing"


# ---------------------------------------------------------------------------
# non-answer content stays out, whatever else is present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("region_type", [
    RegionType.CROSSED_OUT, RegionType.TEACHER_MARKING,
    RegionType.PAGE_FURNITURE, RegionType.PRINTED_TEXT, RegionType.OTHER,
])
@pytest.mark.asyncio
async def test_non_answer_regions_never_reach_composed_evidence(
    db, world, page_image, region_type
):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=region_type)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.source == EvidenceSource.LEGACY_CROPS
        assert result.covered_categories == ()
        assert evidence.student_images.diagram == ["/tmp/legacy-diagram.png"]
        assert workspace.paths == []


@pytest.mark.asyncio
async def test_crossed_out_work_beside_a_table_does_not_become_evidence(
    db, world, page_image
):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(db, world)
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.CROSSED_OUT, reading_order=0)
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.TABLE, reading_order=1)

    with CropWorkspace() as workspace:
        evidence, result = await _build(db, world, response, workspace)
        assert result.region_count == 1, "struck-through working was graded"
        assert len(evidence.student_images.all_paths) == 1


# ---------------------------------------------------------------------------
# the normal automatic workflow is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_exam_with_zero_regions_matches_the_legacy_evidence_exactly(
    db, world, page_image
):
    """No DocumentRegion anywhere: the composed evidence must equal the old one."""
    from backend.grading.evidence import build_grading_evidence

    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    response = await _response_with(
        db, world, table=["/tmp/t.png"], diagram=["/tmp/d.png"],
        answer_text="the recognised answer",
    )

    legacy = build_grading_evidence(
        question=world["q1"], question_response=response, marking_scheme="scheme",
    )
    with CropWorkspace() as workspace:
        composed, result = await _build(db, world, response, workspace)

    assert result.source == EvidenceSource.LEGACY_CROPS
    assert composed == legacy, "the zero-region path diverged from the legacy path"
    assert composed.student_images.all_paths == ["/tmp/t.png", "/tmp/d.png"]


@pytest.mark.asyncio
async def test_the_reference_side_is_untouched_by_composition(db, world, page_image):
    """AUDIT C1 again, now that the student side is merged rather than replaced."""
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    world["q1"].ms_table_images = json.dumps(["/tmp/ms-table.png"])
    world["q1"].ms_diagram_images = json.dumps(["/tmp/ms-diagram.png"])
    await db.commit()
    response = await _response_with(db, world, diagram=["/tmp/legacy-diagram.png"])
    await _add_region(db, world, script, question_id=world["q1"].id,
                      region_type=RegionType.MATH)

    with CropWorkspace() as workspace:
        evidence, _result = await _build(db, world, response, workspace)
        student_paths = set(evidence.student_images.all_paths)
        reference_paths = set(evidence.reference_images.all_paths)

        assert reference_paths == {"/tmp/ms-table.png", "/tmp/ms-diagram.png"}
        assert not (student_paths & reference_paths), (
            "REGRESSION: a student region reached the reference slot"
        )
        assert evidence.reference_images.math == [], (
            "marking-scheme evidence must not acquire a student category"
        )


@pytest.mark.asyncio
async def test_evidence_source_is_logged_without_content(db, world, page_image, caplog):
    script = await _script(db, world)
    await _point_script_at(db, script, page_image)
    question, student = world["q1"], world["student_a"]
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question.id,
        QuestionResponse.student_id == student.id,
    ))).scalars().first()
    response.answer_text = "THE-STUDENTS-PRIVATE-ANSWER"
    await db.commit()
    await _add_region(db, world, script, question_id=question.id)

    with caplog.at_level("INFO", logger="backend.regions.evidence"):
        with CropWorkspace() as workspace:
            await build_evidence(
                question=question, question_response=response,
                exam_id=world["exam_a"].id, student_id=student.id, db=db,
                workspace=workspace, marking_scheme="MARKING-SCHEME-TEXT",
            )
    assert "grading_evidence" in caplog.text
    assert "evidence_source=structured_regions" in caplog.text
    assert "THE-STUDENTS-PRIVATE-ANSWER" not in caplog.text
    assert "MARKING-SCHEME-TEXT" not in caplog.text
