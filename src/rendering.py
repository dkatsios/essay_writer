"""Jinja2 template loading and rendering for agent prompts."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_template_env(templates_dir: str | Path | None = None) -> Environment:
    """Create a Jinja2 environment for prompt templates."""
    path = Path(templates_dir) if templates_dir else _TEMPLATES_DIR
    return Environment(
        loader=FileSystemLoader(str(path)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_name: str, **context: object) -> str:
    """Render a Jinja2 template with the given context."""
    env = create_template_env()
    template = env.get_template(template_name)
    return template.render(**context)
