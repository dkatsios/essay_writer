"""Essay writer tools."""

from src.tools.academic_search import academic_search
from src.tools.crossref_search import crossref_search
from src.tools.docx_builder import build_docx
from src.tools.docx_reader import read_docx
from src.tools.openalex_search import openalex_search
from src.tools.pdf_reader import read_pdf
from src.tools.web_fetcher import fetch_url
from src.tools.word_counter import count_words

__all__ = [
    "academic_search",
    "build_docx",
    "count_words",
    "crossref_search",
    "fetch_url",
    "openalex_search",
    "read_docx",
    "read_pdf",
]
