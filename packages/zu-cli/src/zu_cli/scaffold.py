"""Project scaffolding — the starter `agent.yaml` behind `zu init` and the MCP
`zu_scaffold` tool. One source of truth so the CLI and the coding-agent
integration always write the same thing.

A template is a single self-contained `agent.yaml`: the model, the escalation
ladder (`tiers:` — the tools at each tier, yours or built-in), the checks, and
the task — what + how in one file. Edit the `provider` block to swap models; drop
your own tools in a `tools/` dir beside it and list them in `tiers` as
`tools.my_module:MyTool`.
"""

from __future__ import annotations

import os

_PROVIDER = (
    "provider:\n"
    "  name: anthropic                 # scripted | anthropic | openai-compatible | <module:Class>\n"
    "  model: claude-sonnet-4-6\n"
    "  api_key_env: ANTHROPIC_API_KEY  # the env var NAME — never the key itself\n"
)
_SINK = "event_sink: { driver: sqlite, path: ./zu.db }\n"
_BUDGET = "budget: { max_steps: 20, max_tokens: 200000, wall_time_s: 120 }\n"

# A tier-1/2 web-extraction agent: fetch, fall back to a browser on JS, validate.
_WEB = (
    _PROVIDER
    + "tiers:                            # the escalation ladder — tools at each tier\n"
    "  1: [http_fetch, html_parse]     # cheap: fetch + parse (your own tools go here too)\n"
    "  2: [render_dom]                 # escalate to a real browser when a detector says so\n"
    "plugins:\n"
    "  detectors: [empty, error, js-shell, bot-wall]   # what makes a tier give up\n"
    "  validators: [schema, grounding]\n"
    + _SINK
    + _BUDGET
    + "task:\n"
    "  query: \"Extract the product name and price.\"\n"
    "  target: \"https://example.com/product/123\"\n"
    "  max_tier: 2\n"
    "  output_schema:\n"
    "    type: object\n"
    "    properties:\n"
    "      name: { type: string }\n"
    "      price: { type: string }\n"
    "    required: [name, price]\n"
)

# The smallest agent: a model answers, schema-validated. No tools, no network.
_MINIMAL = (
    _PROVIDER
    + "plugins:\n  validators: [schema]\n"
    + _SINK
    + "task:\n"
    "  query: \"Answer the question as JSON: {\\\"answer\\\": ...}.\"\n"
    "  output_schema:\n"
    "    type: object\n"
    "    properties: { answer: { type: string } }\n"
    "    required: [answer]\n"
)

# A web-research agent: extract several fields from an article page.
_RESEARCH = (
    _PROVIDER
    + "tiers:\n"
    "  1: [http_fetch, html_parse]\n"
    "  2: [render_dom]\n"
    "plugins:\n"
    "  detectors: [empty, error, js-shell, bot-wall]\n"
    "  validators: [schema, grounding]\n"
    + _SINK
    + _BUDGET
    + "task:\n"
    "  query: \"Extract the article's title, author, and publication date.\"\n"
    "  target: \"https://example.com/article\"\n"
    "  max_tier: 2\n"
    "  output_schema:\n"
    "    type: object\n"
    "    properties:\n"
    "      title: { type: string }\n"
    "      author: { type: string }\n"
    "      published: { type: string }\n"
    "    required: [title]\n"
)

TEMPLATES: dict[str, dict[str, str]] = {
    "web": {"agent.yaml": _WEB},
    "minimal": {"agent.yaml": _MINIMAL},
    "research": {"agent.yaml": _RESEARCH},
}

TEMPLATE_NAMES = tuple(TEMPLATES)


def render(template: str) -> dict[str, str]:
    """The ``{filename: content}`` map for a template. Raises KeyError if unknown."""
    return dict(TEMPLATES[template])


def write_template(directory: str, template: str, *, force: bool = False) -> list[str]:
    """Write a template's files into ``directory``. Refuses to overwrite an
    existing file unless ``force`` (so a stray `zu init` never clobbers work).
    Returns the paths written. Raises KeyError (unknown template) or
    FileExistsError (a target exists and not force)."""
    files = render(template)
    os.makedirs(directory, exist_ok=True)
    if not force:
        existing = [n for n in files if os.path.exists(os.path.join(directory, n))]
        if existing:
            raise FileExistsError(", ".join(existing))
    written: list[str] = []
    for name, content in files.items():
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(path)
    return written
