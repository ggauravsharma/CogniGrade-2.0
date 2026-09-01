"""Regression tests for authorized file serving (audit finding C5).

Before this phase, `/uploads` was a public StaticFiles mount: every answer
script, marking scheme and cropped answer image was retrievable by anyone who
knew a URL, with no authentication at all.
"""

from __future__ import annotations

import pytest

from backend.tests.conftest import as_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# the mount itself is gone
# ---------------------------------------------------------------------------

async def test_no_public_uploads_mount_in_app():
    """The application must not re-introduce a StaticFiles mount over uploads.

    Checked three ways, because `app.routes` alone can pass vacuously: newer
    FastAPI stores included routers as opaque wrappers with no `.path`, so a
    naive scan would see nothing either way.
    """
    from starlette.routing import Mount

    from backend.main import app

    mounts = [m.path for m in app.routes if isinstance(m, Mount)]
    assert "/uploads" not in mounts, f"uploads must not be mounted; mounts={mounts}"

    paths = list(app.openapi()["paths"].keys())
    assert not [p for p in paths if p.startswith("/uploads")], "no /uploads routes"

    # and the replacement must actually be wired in
    assert any(p.startswith("/protected-files") for p in paths), (
        "the protected-files router must be included in the application"
    )


async def test_protected_file_routes_are_registered():
    """Guards against the router being dropped from main.py by a later edit."""
    from backend.main import app

    paths = set(app.openapi()["paths"].keys())
    for expected in (
        "/protected-files/exam/{exam_id}/document/{doc_type}",
        "/protected-files/material/{material_id}",
        "/protected-files/answer-script/{answer_script_id}",
        "/protected-files/response/{response_id}/{kind}/{index}",
        "/protected-files/question/{question_id}/marking-image/{kind}/{index}",
    ):
        assert expected in paths, f"missing protected file route: {expected}"


# ---------------------------------------------------------------------------
# anonymous access
# ---------------------------------------------------------------------------

async def test_anonymous_cannot_fetch_answer_script(client, world):
    r = await client.get(f"/protected-files/exam/{world['exam_a'].id}/document/answer_script")
    assert r.status_code == 401, "anonymous users must not retrieve answer scripts"


async def test_anonymous_cannot_fetch_marking_scheme(client, world):
    r = await client.get(f"/protected-files/exam/{world['exam_a'].id}/document/marking_scheme")
    assert r.status_code == 401


async def test_anonymous_cannot_fetch_response_image(client, world):
    r = await client.get(f"/protected-files/response/{world['resp_a'].id}/text/0")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

async def test_student_gets_own_answer_script(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/answer_script",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 student A script", "student A must get their own script"


async def test_student_cannot_request_another_students_script(client, world):
    """The student_id query parameter must be ignored/refused for students."""
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/answer_script"
        f"?student_id={world['student_b'].id}",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "a student must not select another student's script"


async def test_manager_can_fetch_named_students_script(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/answer_script"
        f"?student_id={world['student_b'].id}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 student B script"


async def test_unrelated_professor_cannot_fetch_script(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/answer_script"
        f"?student_id={world['student_a'].id}",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403


async def test_student_denied_marking_scheme_file(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/marking_scheme",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "the marking scheme must never reach a student"


async def test_student_may_read_question_paper(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/question_paper",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 200, "an enrolled student may read the question paper"


async def test_outsider_denied_question_paper(client, world):
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/question_paper",
        headers=as_user(world["outsider"]),
    )
    assert r.status_code == 403, "a non-member must not read the question paper"


# ---------------------------------------------------------------------------
# cropped region images
# ---------------------------------------------------------------------------

async def test_student_gets_own_crop_image(client, world):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/text/0",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 200
    assert r.content == b"\x89PNG crop a"


async def test_student_cannot_get_other_students_crop_image(client, world):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/text/0",
        headers=as_user(world["student_b"]),
    )
    assert r.status_code == 403, "student B must not read student A's cropped answer"


async def test_manager_can_get_crop_image(client, world):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/text/0",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 200


async def test_out_of_range_index_is_404(client, world):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/text/99",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 404, "index beyond the stored list must not leak anything"


async def test_invalid_image_kind_rejected(client, world):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/passwd/0",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 400, "only text/table/diagram are valid kinds"


async def test_student_cannot_read_marking_scheme_image(client, world):
    r = await client.get(
        f"/protected-files/question/{world['q1'].id}/marking-image/diagram/0",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "marking-scheme images are manager-only"


# ---------------------------------------------------------------------------
# path containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attempt", [
    "../../../../etc/passwd",
    "..%2F..%2F..%2Fetc%2Fpasswd",
    "....//....//etc/passwd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
])
async def test_traversal_in_document_type_is_refused(client, world, attempt):
    """No request component reaches the filesystem, so traversal cannot resolve."""
    r = await client.get(
        f"/protected-files/exam/{world['exam_a'].id}/document/{attempt}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code in (400, 404), (
        f"traversal attempt {attempt!r} must not be served (got {r.status_code})"
    )
    assert b"root:" not in r.content


@pytest.mark.parametrize("attempt", ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd"])
async def test_traversal_in_image_kind_is_refused(client, world, attempt):
    r = await client.get(
        f"/protected-files/response/{world['resp_a'].id}/{attempt}/0",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code in (400, 404)
    assert b"root:" not in r.content


async def test_stored_path_outside_upload_root_is_refused(client, world):
    """Defence in depth: even an authorized manager cannot pull a file whose
    stored path escapes the upload root."""
    r = await client.get(
        f"/protected-files/question/{world['q1'].id}/marking-image/diagram/0",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 404, (
        "a database path outside ./uploads must be refused, not served"
    )
    assert b"must never be served" not in r.content


# ---------------------------------------------------------------------------
# individual documents addressed by their own row id  (UI-2)
#
# `POST /exam/save-files` deduplicates by (title, exam, type), so an exam can
# legitimately hold several question-paper files, and the upload UI is
# multi-file. The doc_type route above returns the FIRST match, so nothing could
# address the second one. These routes take a row id instead -- and must apply
# exactly the capability table the doc_type route applies, so that holding an id
# grants nothing that holding the exam id would not.
# ---------------------------------------------------------------------------

async def _material_id(db, exam_id, file_type):
    from sqlalchemy import select

    from backend.models.files import FileTypeEnum, Material

    found = await db.execute(select(Material).where(
        Material.related_exam_id == exam_id,
        Material.file_type == FileTypeEnum(file_type),
    ))
    material = found.scalars().first()
    assert material is not None, f"fixture is missing a {file_type}"
    return material.id


async def _answer_script_id(db, exam_id, student_id):
    from sqlalchemy import select

    from backend.models.files import AnswerScript

    found = await db.execute(select(AnswerScript).where(
        AnswerScript.exam_id == exam_id,
        AnswerScript.student_id == student_id,
    ))
    script = found.scalars().first()
    assert script is not None, "fixture is missing an answer script"
    return script.id


async def test_anonymous_cannot_fetch_material_by_id(client, db, world):
    mid = await _material_id(db, world["exam_a"].id, "question_paper")
    r = await client.get(f"/protected-files/material/{mid}")
    assert r.status_code == 401


async def test_anonymous_cannot_fetch_answer_script_by_id(client, db, world):
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_a"].id)
    r = await client.get(f"/protected-files/answer-script/{sid}")
    assert r.status_code == 401


async def test_manager_gets_marking_scheme_by_id(client, db, world):
    mid = await _material_id(db, world["exam_a"].id, "marking_scheme")
    r = await client.get(
        f"/protected-files/material/{mid}", headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 marking scheme"


async def test_student_denied_marking_scheme_by_id(client, db, world):
    """The id must not become a way around the manager-only rule on this type."""
    mid = await _material_id(db, world["exam_a"].id, "marking_scheme")
    r = await client.get(
        f"/protected-files/material/{mid}", headers=as_user(world["student_a"])
    )
    assert r.status_code == 403, "the marking scheme must never reach a student"


async def test_student_may_read_question_paper_by_id(client, db, world):
    mid = await _material_id(db, world["exam_a"].id, "question_paper")
    r = await client.get(
        f"/protected-files/material/{mid}", headers=as_user(world["student_a"])
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 question paper"


async def test_outsider_denied_question_paper_by_id(client, db, world):
    mid = await _material_id(db, world["exam_a"].id, "question_paper")
    r = await client.get(
        f"/protected-files/material/{mid}", headers=as_user(world["outsider"])
    )
    assert r.status_code == 403


async def test_unrelated_professor_denied_material_by_id(client, db, world):
    """Authority over one exam must not carry into another exam's documents."""
    mid = await _material_id(db, world["exam_a"].id, "marking_scheme")
    r = await client.get(
        f"/protected-files/material/{mid}", headers=as_user(world["other_prof"])
    )
    assert r.status_code == 403


async def test_student_gets_own_answer_script_by_id(client, db, world):
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_a"].id)
    r = await client.get(
        f"/protected-files/answer-script/{sid}", headers=as_user(world["student_a"])
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 student A script"


async def test_student_cannot_fetch_another_students_script_by_id(client, db, world):
    """The whole point of the id form is that holding one grants nothing extra."""
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_b"].id)
    r = await client.get(
        f"/protected-files/answer-script/{sid}", headers=as_user(world["student_a"])
    )
    assert r.status_code == 403


async def test_manager_can_fetch_any_students_script_by_id(client, db, world):
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_b"].id)
    r = await client.get(
        f"/protected-files/answer-script/{sid}", headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 student B script"


async def test_unrelated_professor_denied_answer_script_by_id(client, db, world):
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_a"].id)
    r = await client.get(
        f"/protected-files/answer-script/{sid}", headers=as_user(world["other_prof"])
    )
    assert r.status_code == 403


async def test_outsider_denied_answer_script_by_id(client, db, world):
    sid = await _answer_script_id(db, world["exam_a"].id, world["student_a"].id)
    r = await client.get(
        f"/protected-files/answer-script/{sid}", headers=as_user(world["outsider"])
    )
    assert r.status_code == 403


async def test_missing_ids_are_404(client, world):
    for path in ("/protected-files/material/999999",
                 "/protected-files/answer-script/999999"):
        r = await client.get(path, headers=as_user(world["owner_prof"]))
        assert r.status_code == 404, f"{path} should be 404, got {r.status_code}"


async def test_non_exam_material_types_are_not_served_by_id(client, db, world):
    """An assignment material has no rule here and must not inherit one."""
    from sqlalchemy import select

    from backend.models.files import FileTypeEnum, Material

    found = await db.execute(select(Material).where(
        Material.related_exam_id == world["exam_a"].id,
        Material.file_type == FileTypeEnum.question_paper,
    ))
    material = found.scalars().first()
    material.file_type = FileTypeEnum.assignment_material
    await db.commit()

    r = await client.get(
        f"/protected-files/material/{material.id}", headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 404, "only exam document types are served by this route"


async def test_stored_path_outside_upload_root_is_refused_by_id(client, db, world):
    """The containment guard applies to the id form too, not just doc_type."""
    from sqlalchemy import select

    from backend.models.files import FileTypeEnum, Material

    found = await db.execute(select(Material).where(
        Material.related_exam_id == world["exam_a"].id,
        Material.file_type == FileTypeEnum.marking_scheme,
    ))
    material = found.scalars().first()
    material.file_path = str(world["files"]["outside"])
    await db.commit()

    r = await client.get(
        f"/protected-files/material/{material.id}", headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 404, "a path outside the upload root must never be served"
