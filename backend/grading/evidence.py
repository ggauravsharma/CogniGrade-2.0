"""Provider-neutral description of what a grader is shown.

WHY THIS EXISTS
---------------
Grading has exactly two kinds of input and they must never be confused:

    REFERENCE evidence   what a correct answer looks like
                         (marking scheme text, ideal answer, marking-scheme images)

    STUDENT evidence     what this student actually submitted
                         (extracted answer text, student's table/diagram images)

Before this module, `grade_question_with_diagram` held both in a single
`uploaded_files` list that only ever contained the STUDENT's images, and spliced
that same list into both the marking-scheme slot and the student-answer slot of
the prompt. The grader was therefore shown the student's own diagram as the
reference, and marks came from comparing a drawing with itself.

Keeping the two collections in one immutable structure, with different names and
different types, makes that class of mistake hard to reintroduce: there is no
single variable that could be dropped into both slots.

PROVIDER NEUTRALITY
-------------------
Nothing here imports Gemini, FastAPI, or SQLAlchemy sessions, and nothing here
holds a provider file handle. It is plain paths and plain text, so it is equally
valid input for a future HMER model, a local VLM, or an ensemble. This is
deliberately the shape a `GradingProvider` interface would accept later; it is
NOT that interface yet, and no adapter system is introduced in this phase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


def parse_image_paths(raw: Any) -> list[str]:
    """Normalise a stored image-path field to a list of paths.

    The columns hold a JSON-encoded list in a Text column, so in practice the
    value may be None, an empty string, valid JSON, or malformed JSON written by
    an older code path. Every one of those must degrade to an empty list rather
    than raise: a marking scheme with no images is normal, and a crash here
    would take down grading for the whole question.

    `None -> []` in particular matters, because the previous implementation
    called `len()` on the un-normalised value.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        values: Sequence[Any] = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("malformed image-path field ignored")
            return []
        if not isinstance(parsed, (list, tuple)):
            logger.warning("image-path field was not a list; ignored")
            return []
        values = parsed
    return [v for v in values if isinstance(v, str) and v.strip()]


@dataclass(frozen=True)
class ImageSet:
    """Image paths for one side of the comparison, split by kind."""

    text: list[str] = field(default_factory=list)
    table: list[str] = field(default_factory=list)
    diagram: list[str] = field(default_factory=list)

    @property
    def all_paths(self) -> list[str]:
        """Every path, in a stable order: text, then tables, then diagrams."""
        return [*self.text, *self.table, *self.diagram]

    @property
    def has_any(self) -> bool:
        return bool(self.all_paths)

    @property
    def has_table(self) -> bool:
        return bool(self.table)

    @property
    def has_diagram(self) -> bool:
        return bool(self.diagram)

    def descriptor(self) -> str:
        """Short natural-language name for what is attached, or ''.

        Replaces the previous `check_image_presence` helper, which returned
        three booleans and reported `image_present=True` as its initial value.
        """
        if self.has_diagram and self.has_table:
            return "diagrams and tables"
        if self.has_diagram:
            return "diagrams"
        if self.has_table:
            return "tables"
        if self.text:
            return "images"
        return ""


@dataclass(frozen=True)
class GradingEvidence:
    """Everything a grader needs for one question/student pair."""

    question_text: str
    max_marks: Any

    # reference side
    marking_scheme_text: Optional[str] = None
    ideal_answer_text: Optional[str] = None
    reference_images: ImageSet = field(default_factory=ImageSet)

    # student side
    student_answer_text: Optional[str] = None
    student_images: ImageSet = field(default_factory=ImageSet)

    @property
    def has_reference(self) -> bool:
        return bool(
            self.marking_scheme_text
            or self.ideal_answer_text
            or self.reference_images.has_any
        )

    @property
    def has_student_evidence(self) -> bool:
        return bool(self.student_answer_text or self.student_images.has_any)


def build_grading_evidence(
    *,
    question,
    question_response,
    ideal_answer: Optional[str] = None,
    marking_scheme: Optional[str] = None,
    include_reference_text_images: bool = True,
) -> GradingEvidence:
    """Assemble the evidence for one grading call from ORM rows.

    `question` and `question_response` are read for their image columns only;
    no session work happens here, so this stays unit-testable with simple stubs.

    WHY THE TWO SIDES ARE NOT SYMMETRIC
    -----------------------------------
    The student's TEXT images are deliberately not attached as images: they are
    already converted to `answer_text` upstream by the answer-extraction step,
    so attaching them as well would send the same content twice and inflate
    cost and latency for no gain.

    The marking scheme's text images ARE attached, because the endpoint that is
    supposed to turn them into `ideal_marking_scheme` text
    (`process_marking_scheme_text_image`) is currently broken, so those images
    are frequently the only reference material that exists. `question_response`
    may be None when a student has no recorded response.
    """
    ref_text_images = (
        parse_image_paths(getattr(question, "ms_text_images", None))
        if include_reference_text_images
        else []
    )

    reference_images = ImageSet(
        text=ref_text_images,
        table=parse_image_paths(getattr(question, "ms_table_images", None)),
        diagram=parse_image_paths(getattr(question, "ms_diagram_images", None)),
    )

    student_images = ImageSet(
        text=[],  # see docstring: already represented by the extracted text
        table=parse_image_paths(getattr(question_response, "ans_table_images", None)),
        diagram=parse_image_paths(getattr(question_response, "ans_diagram_images", None)),
    )

    return GradingEvidence(
        question_text=getattr(question, "text", "") or "",
        max_marks=getattr(question, "max_marks", None),
        marking_scheme_text=marking_scheme,
        ideal_answer_text=ideal_answer,
        reference_images=reference_images,
        student_answer_text=getattr(question_response, "answer_text", None),
        student_images=student_images,
    )
