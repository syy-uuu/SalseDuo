"""Reads/renders prompts/*.prompt files — frontmatter (YAML metadata) + plain-text body.

Imported by both src/graph/*.py (runtime) and ops/, tests/eval/*.py (non-runtime). Lives
under prompts/ itself rather than src/: this module's sole responsibility is reading the
folder it lives in, which isn't the same kind of thing as db_client.py/config.py's
"project-wide runtime infrastructure under src/" — if it lived in src/, ops/ would have
to depend on the runtime package just to load a prompt, which is a backwards dependency
direction.

.prompt file format (YAML frontmatter + plain-text body, separated by two `---` lines):
---
name: xxx
version: 1
description: one-line description of what this prompt is for
variables: [foo, bar]      # placeholders {foo}/{bar} used in the body; [] if there are none
---
Body text starts here — plain text all the way down, no extra indentation or escaping
needed, write the prompt however it reads best.

Deployment prerequisite: ops/deploy_model.py's code_paths must include prompts/ in
addition to src/, otherwise router.prompt/finalize.prompt/history_framing.prompt — the
three files used at runtime — won't be bundled into the deployment artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptFile:
    name: str
    version: int
    description: str
    variables: list[str]
    body: str


def load_prompt(name: str) -> PromptFile:
    """Reads prompts/{name}.prompt, splits it into frontmatter + body, and returns the
    raw body (no variable substitution)."""
    path = _PROMPTS_DIR / f"{name}.prompt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    raw = path.read_text(encoding="utf-8")

    if not raw.startswith("---"):
        raise ValueError(f"{path} is missing frontmatter (the file should start with ---)")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path} has malformed frontmatter — expected two --- separators")
    _, frontmatter_raw, body = parts
    meta = yaml.safe_load(frontmatter_raw) or {}

    return PromptFile(
        name=meta.get("name", name),
        version=meta.get("version", 1),
        description=meta.get("description", ""),
        variables=list(meta.get("variables", []) or []),
        body=body.strip("\n"),
    )


def render_prompt(name: str, **kwargs) -> str:
    """After load_prompt, validates that kwargs matches the variables declared in the
    frontmatter one-to-one (too many or too few both raise, never silently ignored — so
    the variables field is actually being enforced, not just documentation), then
    substitutes placeholders via body.format(**kwargs). Prompts with an empty variables
    list also go through this function — just call it with no kwargs; every caller uses
    this single entry point."""
    prompt = load_prompt(name)
    declared = set(prompt.variables)
    provided = set(kwargs.keys())
    if declared != provided:
        problems = []
        missing = declared - provided
        extra = provided - declared
        if missing:
            problems.append(f"missing: {sorted(missing)}")
        if extra:
            problems.append(f"unexpected: {sorted(extra)}")
        raise ValueError(
            f"prompt '{name}' declares variables {sorted(declared)}, "
            f"but was called with {sorted(provided)} ({'; '.join(problems)})"
        )
    return prompt.body.format(**kwargs) if kwargs else prompt.body
