"""The grading prompt.

Moved verbatim out of `geminiAPI.grade_question` / `grade_question_with_diagram`,
where three near-identical copies were built inline inside 200-line handlers.
The wording is unchanged on purpose: this phase is architecture and reliability,
and altering a grading prompt changes marks. Any future rewording bumps
`GRADING_PROMPT_VERSION`.

Provider-neutral: no SDK import. The instruction that asks for JSON comes from
`backend.grading.result.output_instruction`, which is already the domain's own
statement of what a valid grading response looks like.
"""

from __future__ import annotations

from typing import Optional, Tuple

from backend.grading.result import output_instruction

#: Bump when the wording changes in a way that could move marks.
GRADING_PROMPT_VERSION = "grading/v1"

_LENIENCY = (
    "If the marking scheme doesn't specify mark distribution, grade "
    "proportionally based on the level of correctness—giving higher marks for "
    "more accurate and complete answers, and lower marks for partially correct "
    "or incomplete ones. Don't be too strict, nor too lenient."
)


def build_grading_prompt(
    *,
    question_text: str,
    student_answer: str,
    max_marks,
    marking_scheme: Optional[str] = None,
    ideal_answer: Optional[str] = None,
) -> Tuple[str, str]:
    """Build the grading prompt for whichever reference material exists.

    Three shapes, exactly as before: both references, marking scheme only, or
    ideal answer only. The branch is here rather than in a route because which
    reference material exists is a property of the task, not of the transport.
    """
    if marking_scheme and ideal_answer:
        body = (
            f"Question: {question_text}\n\n"
            f"This is the correct marking scheme: {marking_scheme}\n\n"
            f"Ideal Answer: {ideal_answer}\n\n"
            f"Based on these, grade the following student answer: {student_answer}\n\n"
            f"{_LENIENCY}\n\n"
        )
    elif marking_scheme:
        body = (
            f"Question: {question_text}\n\n"
            f"This is the correct marking scheme: {marking_scheme}\n\n"
            f"Grade the following student answer: {student_answer}\n\n"
            f"{_LENIENCY}\n\n"
        )
    else:
        body = (
            f"Question: {question_text}\n\n"
            f"This is the ideal answer: {ideal_answer}\n\n"
            f"Grade the following student answer: {student_answer}\n\n"
            f"{_LENIENCY}\n\n"
        )

    prompt = f"{body}Maximum Marks Possible: {max_marks}.\n{output_instruction(max_marks)}"
    return prompt, GRADING_PROMPT_VERSION


#: Labels for the two image slots in a diagram/table grading call.
#:
#: Audit C1: one list once held only the STUDENT's images and was spliced into
#: both slots, so a diagram was graded against itself. The two sides are named
#: separately here for the same reason `GradingEvidence` names them separately
#: -- there must be no single variable that could land in both.
REFERENCE_IMAGE_HEADING = "[REFERENCE / MARKING SCHEME IMAGES]"
STUDENT_IMAGE_HEADING = "[STUDENT ANSWER IMAGES]"
