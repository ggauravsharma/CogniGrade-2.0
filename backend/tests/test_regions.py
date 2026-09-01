"""Structured page regions: the contract, the gate, and the workflow.

The point of this layer is that a model's guess about where the answers are is
a PROPOSAL, and only a human turns it into an annotation. These tests assert
the properties that claim rests on:

    geometry survives the round trip exactly
    malformed provider output is rejected with a reason, never repaired
    a page index comes from the request, not from the model
    an unclear question assignment stays unassigned rather than guessed
    crossed-out work and teacher markings stay distinguishable
    a model proposal cannot masquerade as an accepted annotation
    the existing crop workflow keeps working untouched

NO LIVE PROVIDER. Segmentation runs against a deterministic fake, so this file
costs zero API quota and cannot flake on a network.
"""

import ast
import json
import pathlib

import pytest
from sqlalchemy import select

from backend.ai.providers.fake_segmentation import (
    FAKE_MODEL,
    MALFORMED_PAGE,
    PAGE_0,
    FakeSegmentationProvider,
)
from backend.ai.segmentation import (
    RegionPrediction,
    SegmentationRequest,
    SegmentationResponse,
    validate_predictions,
)
from backend.models.files import AnswerScript
from backend.models.tables import DocumentRegion, Question, QuestionResponse
from backend.regions.schema import (
    ALLOWED_REGION_TYPES,
    STUDENT_ANSWER_TYPES,
    GeometryKind,
    InvalidRegionError,
    RegionSource,
    RegionStatus,
    RegionType,
    assign_reading_order,
    geometry_bounds,
    normalise_geometry,
    validate_region,
)

from .conftest import as_user

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _rect(x=0.1, y=0.1, w=0.2, h=0.2):
    return {"x": x, "y": y, "w": w, "h": h}


def _ok(**overrides):
    base = dict(
        page_index=0, region_type=RegionType.HANDWRITTEN_TEXT,
        geometry_kind=GeometryKind.RECT, geometry=_rect(), reading_order=0,
    )
    base.update(overrides)
    return validate_region(**base)


# ---------------------------------------------------------------------------
# the contract is provider-neutral
# ---------------------------------------------------------------------------

def test_the_region_domain_imports_nothing_provider_or_framework_specific():
    """A region is a domain fact; it must not depend on a vendor, the web
    layer or the ORM."""
    banned = ("google", "genai", "gemini", "openai", "fastapi", "sqlalchemy", "backend.models")
    for path in sorted((REPO_ROOT / "backend" / "regions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        for name in names:
            for token in banned:
                assert token not in name.lower(), f"{path.name} imports {name}"


def test_the_segmentation_contract_imports_no_sdk():
    tree = ast.parse((REPO_ROOT / "backend" / "ai" / "segmentation.py").read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    for name in names:
        for token in ("google", "genai", "gemini", "fastapi", "sqlalchemy"):
            assert token not in name.lower(), f"segmentation.py imports {name}"


def test_no_route_path_names_a_provider():
    source = (REPO_ROOT / "backend" / "routers" / "regions.py").read_text(encoding="utf-8")
    for token in ("gemini", "google", "genai", "openai"):
        assert f'"/{token}' not in source.lower()
        assert f"_{token}_" not in source.lower().replace("fake_segmentation", "")


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def test_a_rectangle_round_trips_exactly():
    geometry = normalise_geometry(GeometryKind.RECT, _rect(0.125, 0.25, 0.5, 0.125))
    assert geometry == {"x": 0.125, "y": 0.25, "w": 0.5, "h": 0.125}
    assert json.loads(json.dumps(geometry)) == geometry


def test_a_polygon_round_trips_exactly():
    points = [[0.1, 0.2], [0.4, 0.2], [0.45, 0.6], [0.12, 0.58]]
    geometry = normalise_geometry(GeometryKind.POLYGON, {"points": points})
    assert geometry == {"points": points}
    assert json.loads(json.dumps(geometry)) == geometry


def test_a_backwards_drag_is_normalised_not_rejected():
    """Dragging up and to the left is a gesture, not bad data."""
    geometry = normalise_geometry(GeometryKind.RECT, {"x": 0.6, "y": 0.6, "w": -0.2, "h": -0.3})
    assert geometry == {"x": 0.4, "y": 0.3, "w": 0.2, "h": 0.3}


@pytest.mark.parametrize(
    "geometry,code",
    [
        ({"x": 0.5, "y": 0.5, "w": 0.9, "h": 0.1}, "geometry_out_of_bounds"),
        ({"x": -0.5, "y": 0.1, "w": 0.2, "h": 0.2}, "geometry_out_of_bounds"),
        ({"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.2}, "geometry_degenerate"),
        ({"x": 0.1, "y": 0.1, "w": "wide", "h": 0.2}, "geometry_not_numeric"),
        ({"x": 0.1, "y": 0.1}, "geometry_malformed"),
        ("not an object", "geometry_malformed"),
    ],
)
def test_invalid_rectangles_are_rejected_with_a_reason(geometry, code):
    with pytest.raises(InvalidRegionError) as exc:
        normalise_geometry(GeometryKind.RECT, geometry)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "geometry,code",
    [
        ({"points": [[0.1, 0.1], [0.2, 0.2]]}, "geometry_degenerate"),
        ({"points": [[0.1, 0.1], [0.2, 0.2], [0.3, 1.4]]}, "geometry_out_of_bounds"),
        ({"points": [[0.1, 0.1], [0.2, 0.2], [0.3]]}, "geometry_malformed"),
        ({"points": "triangle"}, "geometry_malformed"),
    ],
)
def test_invalid_polygons_are_rejected_with_a_reason(geometry, code):
    with pytest.raises(InvalidRegionError) as exc:
        normalise_geometry(GeometryKind.POLYGON, geometry)
    assert exc.value.code == code


def test_nan_and_infinity_are_rejected():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(InvalidRegionError) as exc:
            normalise_geometry(GeometryKind.RECT, {"x": bad, "y": 0.1, "w": 0.2, "h": 0.2})
        assert exc.value.code in ("geometry_not_finite", "geometry_out_of_bounds")


def test_bounds_are_derived_not_asked_for():
    """A polygon still yields a box, computed deterministically."""
    bounds = geometry_bounds(GeometryKind.POLYGON, {"points": [[0.2, 0.3], [0.6, 0.25], [0.5, 0.7]]})
    assert bounds == {"x": 0.2, "y": 0.25, "w": 0.4, "h": 0.45}


# ---------------------------------------------------------------------------
# region validation
# ---------------------------------------------------------------------------

def test_a_valid_region_is_accepted():
    region = _ok()
    assert region.page_index == 0
    assert region.status == RegionStatus.PROPOSED
    assert region.source == RegionSource.MODEL


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"page_index": -1}, "page_invalid"),
        ({"page_index": "2"}, "page_invalid"),
        ({"region_type": "scribbles"}, "region_type_unknown"),
        ({"status": "maybe"}, "status_unknown"),
        ({"source": "gemini"}, "source_unknown"),
        ({"reading_order": -1}, "reading_order_invalid"),
        ({"reading_order": "first"}, "reading_order_invalid"),
        ({"question_id": "seven"}, "question_invalid"),
        ({"question_part": "   "}, "question_part_invalid"),
    ],
)
def test_invalid_regions_are_rejected(overrides, code):
    with pytest.raises(InvalidRegionError) as exc:
        _ok(**overrides)
    assert exc.value.code == code


def test_a_page_beyond_the_document_is_rejected():
    with pytest.raises(InvalidRegionError) as exc:
        _ok(page_index=7, page_count=3)
    assert exc.value.code == "page_out_of_range"


def test_the_source_vocabulary_names_no_vendor():
    """`source` is structural. Which provider is metadata."""
    for value in RegionSource.ALL:
        for token in ("gemini", "google", "openai", "genai"):
            assert token not in value


def test_reading_order_can_be_renumbered_deterministically():
    regions = [_ok(reading_order=n) for n in (5, 9, 2)]
    renumbered = assign_reading_order([regions[2], regions[0], regions[1]])
    assert [r.reading_order for r in renumbered] == [0, 1, 2]


# ---------------------------------------------------------------------------
# semantic distinctions
# ---------------------------------------------------------------------------

def test_crossed_out_work_is_representable_and_not_answer_content():
    """It must be stored, and it must not be fed to a grader as the answer."""
    region = _ok(region_type=RegionType.CROSSED_OUT)
    assert region.region_type in ALLOWED_REGION_TYPES
    assert region.is_student_answer_content is False


def test_teacher_markings_are_representable_and_not_answer_content():
    region = _ok(region_type=RegionType.TEACHER_MARKING)
    assert region.is_student_answer_content is False


def test_student_content_types_are_answer_content():
    for region_type in STUDENT_ANSWER_TYPES:
        assert _ok(region_type=region_type).is_student_answer_content is True


def test_page_furniture_is_not_answer_content():
    assert _ok(region_type=RegionType.PAGE_FURNITURE).is_student_answer_content is False


def test_a_region_may_be_unassigned():
    """Real content with no confident question is kept, not discarded."""
    region = _ok(question_id=None)
    assert region.question_id is None
    assert region.region_type in ALLOWED_REGION_TYPES


# ---------------------------------------------------------------------------
# the validation gate over provider output
# ---------------------------------------------------------------------------

def _response(predictions, provider="fake"):
    return SegmentationResponse(
        predictions=predictions, provider=provider, model=FAKE_MODEL,
        prompt_version="segmentation/fake-v1",
    )


def test_good_predictions_survive_with_dense_reading_order():
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=3)
    outcome = validate_predictions(_response(PAGE_0), request, question_id_by_number={1: 11, 2: 22})

    assert outcome.rejected_count == 0
    assert [r.reading_order for r in outcome.regions] == list(range(len(PAGE_0)))
    assert all(r.page_index == 0 for r in outcome.regions)


def test_malformed_predictions_are_dropped_and_the_good_ones_kept():
    """The failure mode the gate exists for."""
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=2, page_count=3)
    outcome = validate_predictions(_response(MALFORMED_PAGE), request, question_id_by_number={1: 11, 2: 22})

    assert outcome.accepted_count == 3, [r.region_type for r in outcome.regions]
    assert outcome.rejected_count == 5
    codes = {code for _, code, _ in outcome.rejected}
    assert {"geometry_out_of_bounds", "region_type_unknown", "geometry_degenerate",
            "geometry_malformed"} <= codes
    # reading order stays dense over the SURVIVORS, with no gaps where
    # rejections were
    assert [r.reading_order for r in outcome.regions] == [0, 1, 2]


def test_the_page_index_comes_from_the_request_not_the_model():
    """Providers misreport which page they were looking at."""
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=4, page_count=9)
    lying = RegionPrediction(
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(), raw={"page_index": 99},
    )
    outcome = validate_predictions(_response([lying]), request)
    assert outcome.regions[0].page_index == 4


def test_an_unknown_question_number_leaves_the_region_unassigned():
    """Never attach a student's working to a question that does not exist."""
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=1)
    prediction = RegionPrediction(
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(), question_number=4242, question_part="b",
    )
    outcome = validate_predictions(_response([prediction]), request, question_id_by_number={1: 11})
    assert outcome.accepted_count == 1
    assert outcome.regions[0].question_id is None
    assert outcome.regions[0].question_part is None


def test_a_known_question_number_is_mapped_to_its_id():
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=1)
    prediction = RegionPrediction(
        region_type=RegionType.MATH, geometry_kind=GeometryKind.RECT,
        geometry=_rect(), question_number=2, question_part="a",
    )
    outcome = validate_predictions(_response([prediction]), request, question_id_by_number={2: 77})
    assert outcome.regions[0].question_id == 77
    assert outcome.regions[0].question_part == "a"


def test_provider_confidence_is_stored_but_never_gates_acceptance():
    """Experiments showed self-reported confidence is unreliable."""
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=1)
    low = RegionPrediction(
        region_type=RegionType.DIAGRAM, geometry_kind=GeometryKind.RECT,
        geometry=_rect(), confidence=0.01,
    )
    outcome = validate_predictions(_response([low]), request)
    assert outcome.accepted_count == 1, "a low confidence must not reject a valid region"
    assert outcome.regions[0].metadata["provider_confidence"] == 0.01


def test_a_request_for_a_page_outside_the_document_is_refused():
    with pytest.raises(ValueError):
        SegmentationRequest(page_image_path="/tmp/p.png", page_index=5, page_count=3)


@pytest.mark.asyncio
async def test_the_fake_provider_is_deterministic():
    provider = FakeSegmentationProvider()
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=3)
    first = await provider.segment_page(request)
    second = await provider.segment_page(request)
    assert [p.region_type for p in first.predictions] == [p.region_type for p in second.predictions]
    assert first.model == second.model == FAKE_MODEL


@pytest.mark.asyncio
async def test_the_fake_provider_covers_the_shapes_that_matter():
    provider = FakeSegmentationProvider()
    request = SegmentationRequest(page_image_path="/tmp/p.png", page_index=0, page_count=3)
    response = await provider.segment_page(request)
    types = {p.region_type for p in response.predictions}
    kinds = {p.geometry_kind for p in response.predictions}

    assert {RegionType.CROSSED_OUT, RegionType.TEACHER_MARKING,
            RegionType.DIAGRAM, RegionType.TABLE} <= types
    assert kinds == {GeometryKind.RECT, GeometryKind.POLYGON}
    assert any(p.question_number is None for p in response.predictions), "needs unassigned content"
    assert len({p.question_number for p in response.predictions if p.question_number}) >= 2


def test_the_fake_provider_is_not_a_default():
    """It must be asked for by name, never fallen back to."""
    from backend.ai.providers import get_segmentation_provider

    with pytest.raises(ValueError):
        get_segmentation_provider("")
    with pytest.raises(ValueError):
        get_segmentation_provider("default")


# ---------------------------------------------------------------------------
# production safety: who chooses the segmentation provider
# ---------------------------------------------------------------------------

def test_an_unconfigured_deployment_resolves_no_segmentation_provider():
    """The default deployment must refuse, not substitute."""
    from backend.ai.errors import ProviderNotConfiguredError
    from backend.ai.providers import resolve_segmentation_provider

    with pytest.raises(ProviderNotConfiguredError) as exc:
        resolve_segmentation_provider()
    assert exc.value.category == "not_configured"
    assert exc.value.retryable is False


def test_an_unknown_configured_provider_is_not_configured_either(monkeypatch):
    """A typo in configuration must fail closed, never fall through to a double."""
    from backend.ai.errors import ProviderNotConfiguredError
    from backend.ai.providers import resolve_segmentation_provider

    monkeypatch.setenv("CG_AI__SEGMENTATION__PROVIDER", "not-a-real-adapter")
    with pytest.raises(ProviderNotConfiguredError):
        resolve_segmentation_provider()


def test_the_fake_is_reachable_only_through_configuration(monkeypatch):
    """It stays available to tests -- by the deployment key, not by a request."""
    from backend.ai.providers import resolve_segmentation_provider
    from backend.ai.providers.fake_segmentation import FakeSegmentationProvider

    monkeypatch.setenv("CG_AI__SEGMENTATION__PROVIDER", "fake")
    assert isinstance(resolve_segmentation_provider(), FakeSegmentationProvider)


def test_the_request_body_cannot_name_a_provider():
    """The field existed and defaulted to the fake. It must not come back."""
    from backend.routers.regions import SegmentationRun

    assert "provider" not in SegmentationRun.model_fields
    source = (REPO_ROOT / "backend" / "routers" / "regions.py").read_text(encoding="utf-8")
    assert '"fake"' not in source, "a route names a non-production adapter"


@pytest.mark.asyncio
async def test_segmentation_without_a_configured_provider_writes_nothing(client, db, world):
    """The HIGH-severity regression: a normal production request must not be
    able to persist synthetic regions on a real answer script."""
    script = await _script(db, world)

    res = await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 503, res.text
    assert res.json()["detail"] == "segmentation_not_configured"

    db.expunge_all()
    rows = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.answer_script_id == script.id
    ))).scalars().all()
    assert rows == [], "a request with no configured provider persisted regions"


@pytest.mark.asyncio
async def test_naming_the_fake_in_the_request_body_does_not_select_it(client, db, world):
    """An old client sending the removed field gets the production answer."""
    script = await _script(db, world)

    res = await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3, "provider": "fake"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 503, res.text

    db.expunge_all()
    rows = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.answer_script_id == script.id
    ))).scalars().all()
    assert rows == [], "a request body selected the fake provider"


@pytest.mark.asyncio
async def test_an_unconfigured_run_does_not_delete_existing_proposals(client, db, world):
    """`replace_existing` must not take effect when nothing can replace them."""
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 0, "region_type": "table", "geometry_kind": "rect",
              "geometry": _rect()},
        headers=as_user(world["owner_prof"]),
    )).json()

    res = await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3, "replace_existing": True},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 503

    db.expunge_all()
    row = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.id == created["id"]
    ))).scalars().first()
    assert row is not None, "a refused segmentation run still deleted regions"


# ---------------------------------------------------------------------------
# persistence and the HTTP workflow
# ---------------------------------------------------------------------------

async def _script(db, world, student=None):
    student = student or world["student_a"]
    found = await db.execute(select(AnswerScript).where(
        AnswerScript.exam_id == world["exam_a"].id,
        AnswerScript.student_id == student.id,
    ))
    return found.scalars().first()


@pytest.fixture
def segmentation_configured(monkeypatch):
    """Point the SEGMENTATION task at the deterministic double, explicitly.

    This is the only way the fake becomes reachable: through the same
    deployment configuration key an operator would use, named per test. No
    request body can select it, and a test that forgets this fixture gets the
    production answer -- 503 and nothing written -- rather than silently
    exercising synthetic regions.
    """
    monkeypatch.setenv("CG_AI__SEGMENTATION__PROVIDER", "fake")
    yield "fake"


@pytest.mark.asyncio
async def test_segmentation_persists_proposals_and_reports_rejections(client, db, world, segmentation_configured):
    script = await _script(db, world)
    res = await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 2, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["proposed"] == 3
    assert body["rejected"] == 5
    assert "region_type_unknown" in body["rejected_reasons"]
    assert all(r["status"] == "proposed" and r["source"] == "model" for r in body["regions"])


@pytest.mark.asyncio
async def test_a_stored_region_round_trips_geometry_exactly(client, db, world, segmentation_configured):
    script = await _script(db, world)
    await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )
    res = await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200
    regions = res.json()["regions"]

    polygon = next(r for r in regions if r["geometry_kind"] == "polygon")
    assert polygon["geometry"]["points"] == [[0.15, 0.48], [0.55, 0.46], [0.6, 0.7], [0.18, 0.72]]
    rect = next(r for r in regions if r["geometry_kind"] == "rect")
    assert set(rect["geometry"]) == {"x", "y", "w", "h"}


@pytest.mark.asyncio
async def test_regions_are_returned_in_page_then_reading_order(client, db, world, segmentation_configured):
    script = await _script(db, world)
    for page in (1, 0):  # inserted out of order on purpose
        await client.post(
            f"/answer-scripts/{script.id}/segmentation",
            json={"page_index": page, "page_count": 3},
            headers=as_user(world["owner_prof"]),
        )
    res = await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["owner_prof"])
    )
    regions = res.json()["regions"]
    keys = [(r["page_index"], r["reading_order"]) for r in regions]
    assert keys == sorted(keys)


@pytest.mark.asyncio
async def test_a_human_can_draw_a_region_and_it_is_accepted_immediately(client, db, world):
    script = await _script(db, world)
    res = await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={
            "page_index": 0, "region_type": "diagram", "geometry_kind": "rect",
            "geometry": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.25},
        },
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source"] == "human"
    assert body["status"] == "accepted"


@pytest.mark.asyncio
async def test_a_human_region_with_bad_geometry_is_a_400(client, db, world):
    script = await _script(db, world)
    res = await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={
            "page_index": 0, "region_type": "diagram", "geometry_kind": "rect",
            "geometry": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5},
        },
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 400
    assert "geometry_out_of_bounds" in res.json()["detail"]


@pytest.mark.asyncio
async def test_accepting_a_proposal_without_editing_marks_it_accepted(client, db, world, segmentation_configured):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 1, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )).json()["regions"][0]

    res = await client.patch(
        f"/regions/{created['id']}", json={"status": "accepted"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_editing_a_proposal_marks_it_modified_automatically(client, db, world, segmentation_configured):
    """The record of how good the model was must not depend on the client."""
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 1, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )).json()["regions"][0]

    res = await client.patch(
        f"/regions/{created['id']}",
        json={"geometry": {"x": 0.05, "y": 0.05, "w": 0.4, "h": 0.4}},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "modified"
    assert body["geometry"] == {"x": 0.05, "y": 0.05, "w": 0.4, "h": 0.4}


@pytest.mark.asyncio
async def test_rejecting_a_proposal_keeps_it_as_a_record(client, db, world, segmentation_configured):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 1, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )).json()["regions"][0]

    res = await client.delete(f"/regions/{created['id']}", headers=as_user(world["owner_prof"]))
    assert res.status_code == 200
    assert res.json() == {"id": created["id"], "status": "rejected", "deleted": False}

    db.expunge_all()
    row = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.id == created["id"]
    ))).scalars().first()
    assert row is not None and row.status == "rejected"


@pytest.mark.asyncio
async def test_deleting_a_human_region_removes_it(client, db, world):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 0, "region_type": "table", "geometry_kind": "rect",
              "geometry": _rect()},
        headers=as_user(world["owner_prof"]),
    )).json()

    res = await client.delete(f"/regions/{created['id']}", headers=as_user(world["owner_prof"]))
    assert res.json()["deleted"] is True

    db.expunge_all()
    row = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.id == created["id"]
    ))).scalars().first()
    assert row is None


@pytest.mark.asyncio
async def test_rerunning_segmentation_does_not_destroy_human_work(client, db, world, segmentation_configured):
    """A model re-run replaces its own untouched guesses and nothing else."""
    script = await _script(db, world)
    await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 1, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )
    human = (await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 1, "region_type": "diagram", "geometry_kind": "rect",
              "geometry": _rect(0.5, 0.5, 0.2, 0.2)},
        headers=as_user(world["owner_prof"]),
    )).json()
    proposals = (await client.get(
        f"/answer-scripts/{script.id}/regions?page_index=1",
        headers=as_user(world["owner_prof"]),
    )).json()["regions"]
    accepted = next(r for r in proposals if r["source"] == "model")
    await client.patch(
        f"/regions/{accepted['id']}", json={"status": "accepted"},
        headers=as_user(world["owner_prof"]),
    )

    await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 1, "page_count": 3, "replace_existing": True},
        headers=as_user(world["owner_prof"]),
    )

    after = (await client.get(
        f"/answer-scripts/{script.id}/regions?page_index=1",
        headers=as_user(world["owner_prof"]),
    )).json()["regions"]
    ids = {r["id"] for r in after}
    assert human["id"] in ids, "a human region was destroyed by re-running the model"
    assert accepted["id"] in ids, "an accepted proposal was destroyed by re-running the model"


@pytest.mark.asyncio
async def test_reading_order_can_be_set_explicitly(client, db, world, segmentation_configured):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )).json()["regions"]

    reversed_ids = [r["id"] for r in reversed(created)]
    res = await client.post(
        f"/answer-scripts/{script.id}/regions/reorder",
        json={"region_ids": reversed_ids}, headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    listed = (await client.get(
        f"/answer-scripts/{script.id}/regions?page_index=0",
        headers=as_user(world["owner_prof"]),
    )).json()["regions"]
    assert [r["id"] for r in listed] == reversed_ids
    assert [r["reading_order"] for r in listed] == list(range(len(reversed_ids)))


@pytest.mark.asyncio
async def test_reordering_a_region_from_another_script_is_404(client, db, world):
    script = await _script(db, world)
    other = await _script(db, world, world["student_b"])
    foreign = (await client.post(
        f"/answer-scripts/{other.id}/regions",
        json={"page_index": 0, "region_type": "table", "geometry_kind": "rect",
              "geometry": _rect()},
        headers=as_user(world["owner_prof"]),
    )).json()

    res = await client.post(
        f"/answer-scripts/{script.id}/regions/reorder",
        json={"region_ids": [foreign["id"]]}, headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# multi-page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_question_can_own_regions_on_several_pages(client, db, world):
    """Q5 page 3 region A, page 4 region C -- the contract must allow it."""
    script = await _script(db, world)
    question = world["q1"]

    for page in (0, 1, 2):
        res = await client.post(
            f"/answer-scripts/{script.id}/regions",
            json={
                "page_index": page, "region_type": "handwritten_text",
                "geometry_kind": "rect", "geometry": _rect(0.1, 0.1 * (page + 1), 0.3, 0.08),
                "question_id": question.id, "question_part": "a",
            },
            headers=as_user(world["owner_prof"]),
        )
        assert res.status_code == 201, res.text

    listed = (await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["owner_prof"])
    )).json()["regions"]
    for_question = [r for r in listed if r["question_id"] == question.id]
    assert sorted({r["page_index"] for r in for_question}) == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_region_can_be_unassigned_then_assigned_then_unassigned(client, db, world):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 0, "region_type": "handwritten_text",
              "geometry_kind": "rect", "geometry": _rect()},
        headers=as_user(world["owner_prof"]),
    )).json()
    assert created["question_id"] is None

    assigned = (await client.patch(
        f"/regions/{created['id']}", json={"question_id": world["q1"].id},
        headers=as_user(world["owner_prof"]),
    )).json()
    assert assigned["question_id"] == world["q1"].id

    cleared = (await client.patch(
        f"/regions/{created['id']}", json={"unassign_question": True},
        headers=as_user(world["owner_prof"]),
    )).json()
    assert cleared["question_id"] is None


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_metadata_is_recorded_without_touching_domain_fields(client, db, world, segmentation_configured):
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )).json()["regions"]

    assert all(r["provider"] == "fake" and r["model"] == FAKE_MODEL for r in created)
    assert all(r["prompt_version"] == "segmentation/fake-v1" for r in created)
    # The domain fields are unaffected by provenance.
    assert all(r["source"] == "model" and r["status"] == "proposed" for r in created)


@pytest.mark.asyncio
async def test_no_raw_provider_response_is_persisted(client, db, world, segmentation_configured):
    script = await _script(db, world)
    await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["owner_prof"]),
    )
    db.expunge_all()
    rows = (await db.execute(select(DocumentRegion))).scalars().all()
    for row in rows:
        if row.provider_metadata:
            stored = json.loads(row.provider_metadata)
            assert set(stored) <= {"provider_confidence"}, stored


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_student_may_read_their_own_regions(client, db, world):
    script = await _script(db, world)
    res = await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["student_a"])
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_a_student_may_not_read_another_students_regions(client, db, world):
    script = await _script(db, world, world["student_b"])
    res = await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["student_a"])
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_a_student_may_not_annotate(client, db, world):
    """Reading your own regions is fine; re-annotating what grading runs on is not."""
    script = await _script(db, world)
    res = await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 0, "region_type": "table", "geometry_kind": "rect",
              "geometry": _rect()},
        headers=as_user(world["student_a"]),
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_a_student_may_not_request_segmentation(client, db, world, segmentation_configured):
    script = await _script(db, world)
    res = await client.post(
        f"/answer-scripts/{script.id}/segmentation",
        json={"page_index": 0, "page_count": 3},
        headers=as_user(world["student_a"]),
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_an_outsider_gets_nothing(client, db, world, segmentation_configured):
    script = await _script(db, world)
    for method, url, payload in (
        ("get", f"/answer-scripts/{script.id}/regions", None),
        ("post", f"/answer-scripts/{script.id}/segmentation",
         {"page_index": 0, "page_count": 3}),
    ):
        call = getattr(client, method)
        res = await (call(url, headers=as_user(world["outsider"])) if payload is None
                     else call(url, json=payload, headers=as_user(world["outsider"])))
        assert res.status_code in (403, 404), (url, res.status_code)


@pytest.mark.asyncio
async def test_a_manager_of_another_classroom_cannot_reach_these_regions(client, db, world):
    """Cross-classroom isolation: authority over exam B grants nothing on exam A."""
    script = await _script(db, world)
    res = await client.get(
        f"/answer-scripts/{script.id}/regions", headers=as_user(world["other_prof"])
    )
    assert res.status_code in (403, 404)


@pytest.mark.asyncio
async def test_editing_a_region_resolves_authorization_from_the_region(client, db, world):
    """Being a manager somewhere must not grant edit rights everywhere."""
    script = await _script(db, world)
    created = (await client.post(
        f"/answer-scripts/{script.id}/regions",
        json={"page_index": 0, "region_type": "table", "geometry_kind": "rect",
              "geometry": _rect()},
        headers=as_user(world["owner_prof"]),
    )).json()

    res = await client.patch(
        f"/regions/{created['id']}", json={"region_type": "diagram"},
        headers=as_user(world["other_prof"]),
    )
    assert res.status_code in (403, 404)


@pytest.mark.asyncio
async def test_anonymous_access_is_refused(client, db, world):
    script = await _script(db, world)
    res = await client.get(f"/answer-scripts/{script.id}/regions")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_a_missing_answer_script_is_404(client, world):
    res = await client.get("/answer-scripts/999999/regions", headers=as_user(world["owner_prof"]))
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# the existing crop workflow is untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_legacy_crop_columns_still_work(db, world):
    """Additive means additive: crops keep working with no regions at all."""
    response = (await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == world["q1"].id,
        QuestionResponse.student_id == world["student_a"].id,
    ))).scalars().first()

    assert response.ans_text_images is not None
    assert json.loads(response.ans_text_images)

    regions = (await db.execute(select(DocumentRegion))).scalars().all()
    assert regions == [], "the fixture world has no regions, and grading still works"


def test_the_legacy_buckets_map_onto_the_new_vocabulary():
    from backend.regions.schema import LEGACY_BUCKET_TO_REGION_TYPE

    assert set(LEGACY_BUCKET_TO_REGION_TYPE) == {"text", "table", "diagram"}
    for value in LEGACY_BUCKET_TO_REGION_TYPE.values():
        assert value in ALLOWED_REGION_TYPES
