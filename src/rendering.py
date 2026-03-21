"""Jinja2 template loading and rendering for agent prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=4)
def _get_env(templates_dir: str) -> Environment:
    """Return a cached Jinja2 environment for the given directory."""
    return Environment(
        loader=FileSystemLoader(templates_dir),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_name: str, **context: object) -> str:
    """Render a Jinja2 template with the given context."""
    env = _get_env(str(_TEMPLATES_DIR))
    template = env.get_template(template_name)
    return template.render(**context)
