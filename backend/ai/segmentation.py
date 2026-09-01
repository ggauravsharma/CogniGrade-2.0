"""The segmentation task: ask a provider where the regions of a page are.

Provider-independent request/response shapes, plus the deterministic gate every
proposal must pass before it can be persisted.

PROPOSALS, NOT ANNOTATIONS
--------------------------
Segmentation experiments on real answer sheets found this class of model
silently omits content, occasionally merges adjacent regions, reports page
indices that do not match the page it was given, and self-reports confidence
and "did I miss anything" flags that do not correlate with being right.

So the output of a provider is a PROPOSAL. `validate_predictions` is the seam
where that proposal meets facts the application already knows -- which page was
sent, how many pages the document has, what the region vocabulary is -- and
anything that disagrees is dropped with a reason rather than trusted.

Two rules follow from those failure modes and are enforced here:

* the page index is taken from the REQUEST, never from the response. We know
  which page we sent; asking a model to tell us is inviting it to be wrong
  about something we already have.
* a proposal is never given a question assignment the provider did not state
  clearly. Guessing puts a student's working under the wrong question.

No SDK import: this module is the contract, not an adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.regions.schema import (
    InvalidRegionError,
    Region,
    RegionSource,
    RegionStatus,
    validate_region,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentationRequest:
    """One page, handed to a provider for region proposals.

    `page_image_path` is a LOCAL path, as everywhere else in the AI layer: the
    caller never holds a provider file handle, so an upload cannot escape the
    adapter uncleaned.
    """

    page_image_path: str
    page_index: int
    #: Total pages in the document, so a response claiming page 9 of a 3-page
    #: script can be rejected deterministically.
    page_count: int
    #: Question numbers that exist on this exam, when known. Used only to
    #: REJECT assignments to questions that do not exist -- never to invent one.
    known_question_numbers: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.page_index < 0 or self.page_index >= max(1, self.page_count):
            raise ValueError(
                f"page_index {self.page_index} outside a {self.page_count}-page document"
            )


@dataclass(frozen=True)
class RegionPrediction:
    """One region as a provider described it, before validation.

    Deliberately permissive: this is what arrived, not what is storable. Every
    field is optional or loosely typed precisely so that malformed output can be
    represented, inspected and rejected with a reason instead of exploding at
    the parse step.
    """

    region_type: Any = None
    geometry_kind: Any = None
    geometry: Any = None
    question_number: Any = None
    question_part: Any = None
    #: Whatever the provider said about itself. Stored as opaque metadata and
    #: never used to accept or reject -- see the module docstring.
    confidence: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentationResponse:
    """What a provider returned for one page."""

    predictions: Sequence[RegionPrediction]
    provider: str
    model: str
    prompt_version: str = "unversioned"
    duration_ms: int = 0


@dataclass(frozen=True)
class ValidationOutcome:
    """The result of putting a response through the deterministic gate."""

    regions: List[Region]
    #: `(index_in_response, error_code, message)` for each dropped prediction.
    rejected: List[Tuple[int, str, str]] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.regions)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def validate_predictions(
    response: SegmentationResponse,
    request: SegmentationRequest,
    *,
    question_id_by_number: Optional[Dict[int, int]] = None,
) -> ValidationOutcome:
    """Turn provider output into storable proposals, dropping what cannot be.

    Returns every region that survived, in response order, with dense reading
    order assigned from that order -- a deterministic fact, not something the
    provider is asked for.

    `question_id_by_number` maps the question NUMBERS a provider can see on the
    page to the database ids it cannot. An assignment to a number that is not in
    the map is discarded (the region is kept, unassigned) rather than guessed.
    """
    regions: List[Region] = []
    rejected: List[Tuple[int, str, str]] = []
    mapping = question_id_by_number or {}

    for index, prediction in enumerate(response.predictions):
        question_id = None
        question_part = prediction.question_part if isinstance(prediction.question_part, str) else None

        number = prediction.question_number
        if isinstance(number, bool):
            number = None
        elif isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if isinstance(number, int):
            question_id = mapping.get(number)
            if question_id is None:
                # The provider named a question this exam does not have. Keep
                # the region -- the content is real -- but leave it unassigned
                # rather than attach it to the wrong thing.
                logger.info(
                    "segmentation proposed an unknown question number; keeping the "
                    "region unassigned (page_index=%s)", request.page_index,
                )
                question_part = None

        metadata: Dict[str, Any] = {}
        if prediction.confidence is not None:
            # Stored, never acted on.
            metadata["provider_confidence"] = prediction.confidence

        try:
            region = validate_region(
                # The page comes from the REQUEST. A provider-supplied page
                # index is ignored entirely: we know what we sent.
                page_index=request.page_index,
                region_type=prediction.region_type,
                geometry_kind=prediction.geometry_kind,
                geometry=prediction.geometry,
                reading_order=len(regions),
                status=RegionStatus.PROPOSED,
                source=RegionSource.MODEL,
                question_id=question_id,
                question_part=question_part,
                metadata=metadata,
                page_count=request.page_count,
            )
        except InvalidRegionError as exc:
            rejected.append((index, exc.code, exc.message))
            continue

        regions.append(region)

    if rejected:
        logger.warning(
            "segmentation: %s of %s proposals rejected on page %s (%s)",
            len(rejected), len(response.predictions), request.page_index,
            sorted({code for _, code, _ in rejected}),
        )
    return ValidationOutcome(regions=regions, rejected=rejected)


class SegmentationProvider:
    """The interface a segmentation adapter implements.

    Mirrors `TextTaskProvider`: one attempt, provider-neutral in and out, and
    `ProviderError` on failure. Retry, timeout and telemetry stay the service
    layer's business so an adapter cannot get them wrong.
    """

    name: str = "unset"

    async def segment_page(self, request: SegmentationRequest) -> SegmentationResponse:  # pragma: no cover - interface
        raise NotImplementedError
