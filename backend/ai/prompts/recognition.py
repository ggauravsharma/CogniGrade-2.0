"""Handwriting-recognition prompts: student answers and marking-scheme images.

Moved verbatim from `extract_single_answer_text`,
`process_answer_text_images_logic` and `process_marking_scheme_text_image`.

These are the prompts a specialist HTR/HMER provider would eventually replace
entirely rather than reword — which is exactly why they belong behind a task
name (`AITask.ANSWER_RECOGNITION`) instead of inside a route.
"""

from __future__ import annotations

from typing import Tuple

ANSWER_RECOGNITION_VERSION = "answer_recognition/v1"
MARKING_SCHEME_RECOGNITION_VERSION = "marking_scheme_recognition/v1"


def build_answer_recognition_prompt() -> Tuple[str, str]:
    """One student's answer images, read into text."""
    prompt = """
Task: The given image shows a student's handwritten answer with its Question Number at the top ”).
1) Read and extract that Question Number.
2) Extract the full answer text, breaking out any sub-parts.
3) Carefully consider the context of each answer to avoid extraction errors that may result from poor handwriting. For eg, simple spelling errors or subscript/superscript errors can be corrected, but do not correct calculation errors or the final answer.
4) Return:

Question Number [question_number]
Answer: [text]          ← if no sub-parts

—or—

Question Number [question_number]
Part: [part_label] - Answer: [text]
"""
    return prompt, ANSWER_RECOGNITION_VERSION


def build_batch_answer_recognition_prompt() -> Tuple[str, str]:
    """Several answer images in one call; sections separated by blank lines."""
    prompt = """
Task: Each image shows a student’s handwritten answer with its Question Number at the top ”).
1) Read extract that Question Number.
2) Extract the full answer text, breaking out any sub-parts.
3) Carefully consider the context of each answer to avoid extraction errors that may result from poor handwriting.
4) Return:

Question Number [question_number]
Answer: [text]          ← if no sub-parts

—or—

Question Number [question_number]
Part: [part_label] - Answer: [text]
...

Separate each question with a blank line.
"""
    return prompt, ANSWER_RECOGNITION_VERSION


def build_marking_scheme_recognition_prompt() -> Tuple[str, str]:
    """Several marking-scheme images, each addressed by a unique key.

    The key is how a batched response is mapped back to the right question, so
    the format is load-bearing rather than cosmetic.
    """
    prompt = """Task: You are provided with multiple images, each containing marking scheme information for an exam question.
Each image has a unique key.

Your task is to extract and clearly structure the marking scheme details from each image.
For each image, follow this format strictly:
Key: <key>
[Extracted marking scheme details here]

If the marking scheme includes multiple criteria or parts, list each one on a new line using the format:
Key: <key>
Question Number [Question Number] - [extracted text]
Part: [part number] - Details: [extracted text]
Part: [part number] - Details: [extracted text]
...

If the image contains a single cohesive marking scheme, simply output the full details under the key.
"""
    return prompt, MARKING_SCHEME_RECOGNITION_VERSION
