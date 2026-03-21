"""Word counting tool for essay sections."""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool


@tool
def count_words(
    text: Annotated[str, "The text to count words in."],
) -> int:
    """Count the number of words in the given text. Handles Greek and Latin scripts."""
    return len(text.split())
