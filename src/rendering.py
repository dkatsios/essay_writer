"""Jinja2 template loading and rendering for agent prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SPLIT_MARKER = "<!-- SPLIT -->"


class PromptPair(NamedTuple):
    """A rendered prompt split into system and user messages."""

    system: str | None
    user: str


@lru_cache(maxsize=4)
def _get_env(templates_dir: str) -> Environment:
    """Return a cached Jinja2 environment for the given directory."""
    return Environment(
        loader=FileSystemLoader(templates_dir),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_name: str, **context: object) -> PromptPair:
    """Render a Jinja2 template and split into system/user messages.

    Templates may contain a ``<!-- SPLIT -->`` marker.  Text before the marker
    becomes the *system* message; text after becomes the *user* message.  If the
    marker is absent the entire rendered text is used as the *user* message with
    ``system=None``.
    """
    env = _get_env(str(_TEMPLATES_DIR))
    template = env.get_template(template_name)
    rendered = template.render(**context)
    if _SPLIT_MARKER in rendered:
        system, user = rendered.split(_SPLIT_MARKER, 1)
        return PromptPair(system.strip(), user.strip())
    return PromptPair(None, rendered)
