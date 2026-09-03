"""What the application asks an AI provider for, and what it gets back.

Provider-neutral by construction: no SDK import, no FastAPI, no SQLAlchemy, no
`backend.models` (asserted by a test). A local VLM, a specialist HTR model, an
ensemble or a human service can satisfy these shapes unchanged.

WHY TASK NAMES AND NOT JUST "generate"
--------------------------------------
`AITask` is the unit that configuration, model selection, prompt versioning and
telemetry are all keyed by. A generic `generate(prompt)` would leave every one
of those with nowhere to hang: you could not point HTR at a specialist model
while grading stays on a general one, which is precisely the split this
architecture exists to allow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Tuple


class AITask:
    """The AI tasks CogniGrade actually performs today.

    Deliberately the current five, not a speculative catalogue. Later phases
    add SEGMENTATION, STRUCTURE, VERIFICATION and so on as they become real.
    """

    #: Read a question paper / solution script / marking scheme document.
    DOCUMENT_EXTRACTION = "document_extraction"
    #: Read the question-label hierarchy out of a question paper.
    LABEL_EXTRACTION = "label_extraction"
    #: Read a student's handwritten answer images.
    ANSWER_RECOGNITION = "answer_recognition"
    #: Read a WHOLE answer script and say which of the exam's existing
    #: questions each answer belongs to. The stage between "a script was
    #: uploaded" and "there is something per question to grade", which the
    #: product previously had no automatic path for -- a student had to cut the
    #: script up by hand before the AI was allowed to run.
    ANSWER_MAPPING = "answer_mapping"
    #: Read marking-scheme images.
    MARKING_SCHEME_RECOGNITION = "marking_scheme_recognition"
    #: Award a mark for one answer.
    GRADING = "grading"
    #: Propose the regions of a page: where the answers, diagrams, tables,
    #: crossed-out work and teacher markings are. Added because segmentation is
    #: now a real task with a real provider interface, not a placeholder.
    SEGMENTATION = "segmentation"

    ALL = (
        DOCUMENT_EXTRACTION,
        LABEL_EXTRACTION,
        ANSWER_RECOGNITION,
        ANSWER_MAPPING,
        MARKING_SCHEME_RECOGNITION,
        GRADING,
        SEGMENTATION,
    )


class FinishReason:
    """Why a provider stopped generating, named for the concept not the vendor.

    Small on purpose. These are the distinctions that change what CogniGrade
    should DO about a failure -- a truncated response is a request-shape
    problem, a blocked one is a content problem, and a malformed complete one
    is model variance. Anything finer is a vendor's taxonomy and belongs in
    the adapter that speaks it.
    """

    #: The model finished on its own terms.
    COMPLETE = "complete"
    #: An output limit cut the response off. The text that came back is partial.
    TRUNCATED = "truncated"
    #: Safety, policy or recitation stopped it.
    BLOCKED = "blocked"
    #: The provider gave a reason this application does not model.
    OTHER = "other"
    #: The provider gave no reason, or it could not be read.
    UNKNOWN = "unknown"

    ALL = (COMPLETE, TRUNCATED, BLOCKED, OTHER, UNKNOWN)


@dataclass(frozen=True)
class TextPart:
    """A literal piece of prompt text."""

    text: str


@dataclass(frozen=True)
class FilePart:
    """A LOCAL file to include at this position in the prompt.

    A path, never a provider handle. Whether the provider uploads it, streams
    it or reads it from disk is the adapter's business, and keeping handles out
    of the caller's hands is what stops them leaking out of routes uncleaned.
    """

    path: str


@dataclass(frozen=True)
class ProviderRequest:
    """One provider invocation, described without naming a provider.

    `parts` is ORDERED and may interleave text and files. That is not
    decoration: diagram grading positions the marking-scheme images
    immediately after the marking-scheme text and the student's images
    immediately after the student's answer, and flattening that into
    "all files, then all text" would change what the model is being asked.
    """

    task: str
    parts: Tuple[Any, ...]
    #: Ask the provider for machine-readable JSON where it supports doing so.
    expects_json: bool = False
    #: Which prompt template produced this request; carried into telemetry so a
    #: later experiment log can say what generated a given result.
    prompt_version: str = "unversioned"

    def __post_init__(self) -> None:
        if self.task not in AITask.ALL:
            raise ValueError(f"unknown AI task: {self.task!r}")
        for part in self.parts:
            if not isinstance(part, (TextPart, FilePart)):
                raise TypeError(f"prompt parts must be TextPart or FilePart, got {type(part).__name__}")

    @classmethod
    def simple(
        cls,
        *,
        task: str,
        prompt: str,
        file_paths: Sequence[str] = (),
        expects_json: bool = False,
        prompt_version: str = "unversioned",
    ) -> "ProviderRequest":
        """The common shape: some files, then one block of text."""
        parts = tuple(FilePart(path) for path in file_paths if path) + (TextPart(prompt),)
        return cls(
            task=task, parts=parts, expects_json=expects_json,
            prompt_version=prompt_version,
        )

    @property
    def file_paths(self) -> Tuple[str, ...]:
        return tuple(part.path for part in self.parts if isinstance(part, FilePart))

    @property
    def prompt_text(self) -> str:
        """The text alone, for logging length or debugging. Never logged raw."""
        return chr(10).join(part.text for part in self.parts if isinstance(part, TextPart))


@dataclass(frozen=True)
class ProviderResponse:
    """The text a provider produced, plus what it cost to get it."""

    text: str
    provider: str
    model: str
    task: str
    prompt_version: str
    attempts: int = 1
    duration_ms: int = 0
    #: Provider file identifiers created and then cleaned up for this call.
    #: Recorded for debugging only; they are already deleted by the time a
    #: caller sees this.
    uploaded_file_count: int = 0
    warnings: Sequence[str] = field(default_factory=tuple)
    #: WHY generation stopped, in this application's own vocabulary -- see
    #: `FinishReason`. Every generative provider has this concept and every one
    #: names it differently, so the adapter translates and nothing above the
    #: adapter learns a vendor's spelling.
    #:
    #: It exists because it was thrown away. A response that stops at an output
    #: limit still returns its partial text, so a truncated answer reaches the
    #: strict grading decoder as invalid JSON and is recorded as
    #: `malformed_json` -- indistinguishable from a model that simply wrote
    #: something malformed. The provider knew which it was; we did not keep it.
    finish_reason: Optional[str] = None
    #: Tokens the provider reported for this call, when it reports them. Only
    #: counts: a token count cannot carry a student's answer.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
