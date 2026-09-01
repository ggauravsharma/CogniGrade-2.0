"""Task prompts, out of the route bodies and under version labels.

Each builder returns `(text, version)`. The version travels with the request
into telemetry, so a later experiment log can answer "which prompt produced
this mark" without anyone having to guess from a commit date.

Versions are bumped BY HAND when the wording changes in a way that could move
results. That is the whole mechanism -- deliberately not a prompt-management
service, and deliberately not a database table.
"""

from backend.ai.prompts.extraction import (
    DOCUMENT_EXTRACTION_VERSION,
    LABEL_EXTRACTION_VERSION,
    build_document_extraction_prompt,
    build_label_extraction_prompt,
)
from backend.ai.prompts.grading import (
    GRADING_PROMPT_VERSION,
    build_grading_prompt,
)
from backend.ai.prompts.recognition import (
    ANSWER_RECOGNITION_VERSION,
    MARKING_SCHEME_RECOGNITION_VERSION,
    build_answer_recognition_prompt,
    build_batch_answer_recognition_prompt,
    build_marking_scheme_recognition_prompt,
)

__all__ = [
    "DOCUMENT_EXTRACTION_VERSION",
    "LABEL_EXTRACTION_VERSION",
    "GRADING_PROMPT_VERSION",
    "ANSWER_RECOGNITION_VERSION",
    "MARKING_SCHEME_RECOGNITION_VERSION",
    "build_document_extraction_prompt",
    "build_label_extraction_prompt",
    "build_grading_prompt",
    "build_answer_recognition_prompt",
    "build_batch_answer_recognition_prompt",
    "build_marking_scheme_recognition_prompt",
]
