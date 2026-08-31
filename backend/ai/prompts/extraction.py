"""Document-reading prompts: question labels and marking-scheme text.

Moved verbatim out of `upload_and_extract` and `extract_question_labels`, where
they sat as multi-hundred-character literals inside handler bodies alongside
file IO, authorization and database writes. Wording unchanged; only the home.
"""

from __future__ import annotations

from typing import Sequence, Tuple

LABEL_EXTRACTION_VERSION = "label_extraction/v1"
DOCUMENT_EXTRACTION_VERSION = "document_extraction/v1"


def build_label_extraction_prompt() -> Tuple[str, str]:
    """Read the question-label hierarchy and top-level marks from a paper."""
    prompt = """
Extract every question label from the paper in the form Question_Number.Part.Subpart (any depth),
that is, parent questions are separated from their children by points '.'. There may be multiple levels of hierarchy.
For each top-level question (no dots), also extract its maximum marks as 'Max Marks - X'.
Do not attach marks to any sub-parts. Extract only numeric value of the Question Number, for example Q1 should be extracted as 1.

Example output:
1 - Max Marks - 6
1.1
1.1.a
1.2
2 - Max Marks - 5
2.1
2.1.a
2.1.b
"""
    return prompt, LABEL_EXTRACTION_VERSION


def build_document_extraction_prompt(leaf_labels: Sequence[str]) -> Tuple[str, str]:
    """Read a marking scheme / solution script, guided by known question labels.

    `leaf_labels` is the hierarchy already extracted from the question paper; it
    tells the model what structure to expect rather than asking it to invent one.
    """
    prompt = f"""You are given a marking scheme/solution script from which you have to extract text.
                The structure of the marking scheme should roughly follow the following questions and parts hierarchy:
{list(leaf_labels)}

The labels are given in the format Question.Part.Subpart, that is, the parent questions are separated from their children by points '.'.

Task:
  Using these given labels as reference, locate and extract the solution or marking-scheme section—complete with its mark allocation, as well as possile. Preserve:
  - original formatting (spacing, bullets, arrows, code blocks, annotations)
  - layout and spatial cues
  - any explicit mark numbers

Extraction Instructions:
1. Group Question.parts under a single Question
2. If marks are mentioned, capture them and associate them with the exact text.
3. Retain any question number you see (e.g. “Question Number - X”).
4. Do not alter content—no corrections, just copy formatting verbatim.
5. Skip any strikethrough or scribbled-out text (and their marks).
6. Ignore any extraneous text not directly part of a solution or its marks.
7. Focus solely on extracting the marking-scheme/solutions. Do not extract any question text even if it is present in the document.
8. Give output in Markdown Format, do not use JSON formatting.

Output format for each extracted question:
---
For each top-level question Q:
    Question Number - Q  Max Marks - M
If Q has parts, nest them under Q:
    Part Q.a  (Add Partial Marks - m if available)
    <exact text of sub-question a>
    Part Q.b  (Add Partial Marks - n if available)
    <exact text of sub-question b>
    ...
---

"""
    return prompt, DOCUMENT_EXTRACTION_VERSION
