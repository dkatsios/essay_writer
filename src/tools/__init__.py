"""Essay writer tools.

Keep package import lightweight so importing one tool submodule does not pull in
the whole document/research stack during web startup.
"""

from __future__ import annotations

from importlib import import_module


_LAZY_EXPORTS = {
    "build_document": ("src.tools.docx_builder", "build_document"),
    "fetch_url_content": ("src.tools.web_fetcher", "fetch_url_content"),
    "read_docx_text": ("src.tools.docx_reader", "read_docx_text"),
    "read_pdf_text": ("src.tools.pdf_reader", "read_pdf_text"),
    "run_research": ("src.tools.research_sources", "run_research"),
}

__all__ = [
    "build_document",
    "fetch_url_content",
    "read_docx_text",
    "read_pdf_text",
    "run_research",
]


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
