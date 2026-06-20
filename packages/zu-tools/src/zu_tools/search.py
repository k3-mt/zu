"""web_search — a tier-1 search tool with pluggable connectors.

The agent's *first* move on an open-web task: turn a question into a short list
of candidate pages (title + url) it can then ``http_fetch`` / ``render_dom``.
Search is itself retrieval, so the tool surfaces its results under a ``content``
key — that makes the loop store them as a ``data.source.fetched`` event, so a url
the agent later reports (e.g. the booking page it picked) is *grounded* in what
search actually returned, not invented.

Connectors are pluggable: ``WebSearch(connector="exa")`` today, more on
expansion. A connector is anything with ``async search(query, num_results) ->
list[{"title", "url"}]`` and a ``host`` (so the tool's egress is the connector's
single API host — not the open egress a page fetcher needs). The default is
:class:`ExaConnector` (``api.exa.ai``), keyed by ``EXA_API_KEY``. As with
``http_fetch``, the live HTTP call sits behind an injectable httpx transport, so
the tool is exercised hermetically with mocked results and no key.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from zu_core.ports import CAP_NET


class SearchConnector(Protocol):
    """A search backend: a single API ``host`` and an async ``search``."""

    host: str

    async def search(self, query: str, num_results: int) -> list[dict[str, str]]: ...


class ExaConnector:
    """Exa (``api.exa.ai``) — POST /search with an ``x-api-key`` header.

    The key is read from ``api_key_env`` at call time (so a bundle's ``.env``,
    loaded into the environment for the run, supplies it). ``transport`` is the
    testability seam: an ``httpx.MockTransport`` returns canned results offline.
    ``trust_env`` is on, so inside the sandbox the call routes through the egress
    proxy like every other off-box request."""

    host = "api.exa.ai"
    url = "https://api.exa.ai/search"

    def __init__(
        self,
        *,
        api_key_env: str = "EXA_API_KEY",
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key_env = api_key_env
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    def _key(self) -> str:
        key = self._api_key or os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(
                f"no Exa API key: set ${self.api_key_env} "
                "(a bundle's .env is loaded for the run)"
            )
        return key

    async def search(self, query: str, num_results: int) -> list[dict[str, str]]:
        async with httpx.AsyncClient(
            timeout=self._timeout, trust_env=True, transport=self._transport
        ) as c:
            r = await c.post(
                self.url,
                headers={"x-api-key": self._key(), "content-type": "application/json"},
                json={"query": query, "numResults": num_results},
            )
            r.raise_for_status()
            data = r.json()
        out: list[dict[str, str]] = []
        for item in data.get("results", []):
            url = item.get("url") or ""
            if url:
                out.append({"title": item.get("title") or "", "url": url})
        return out


_CONNECTORS: dict[str, type[Any]] = {"exa": ExaConnector}


class WebSearch:
    name = "web_search"
    tier = 1  # cheap; the first move on an open-web task
    schema = {
        "name": "web_search",
        "description": "Search the web and return the top results (title + url).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {
                    "type": "integer",
                    "description": "how many results to return (optional)",
                },
            },
            "required": ["query"],
        },
    }
    prompt_fragment = (
        "web_search(query): search the web, returns top results (title + url). "
        "Use it first to find the right page, then http_fetch/render_dom that url."
    )
    # Search talks to ONE API host, so unlike a page fetcher it declares a scoped
    # egress (the connector's host), not EGRESS_OPEN — a tight boundary the sandbox
    # proxy can enforce. No fs/subprocess capability.
    capabilities = frozenset({CAP_NET})

    def __init__(
        self,
        connector: str | SearchConnector = "exa",
        *,
        num_results: int = 5,
        api_key: str | None = None,
        api_key_env: str = "EXA_API_KEY",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        conn: SearchConnector
        if isinstance(connector, str):
            conn_cls = _CONNECTORS.get(connector)
            if conn_cls is None:
                raise ValueError(
                    f"unknown search connector {connector!r}; have {sorted(_CONNECTORS)}"
                )
            conn = conn_cls(api_key_env=api_key_env, api_key=api_key, transport=transport)
        else:
            conn = connector
        self._connector = conn
        self.num_results = num_results
        # Egress reflects the connector's single API host — set per instance.
        self.egress = frozenset({getattr(conn, "host", "*")})

    async def __call__(self, ctx: Any, query: str, num_results: int | None = None) -> dict:
        results = await self._connector.search(query, num_results or self.num_results)
        # Surface results as `content` too, so the loop records them as retrieved
        # provenance: a url the agent later reports is grounded in what search
        # returned, not fabricated.
        lines = [f"Search results for {query!r}:"]
        lines += [f"{i}. {r['title']} — {r['url']}" for i, r in enumerate(results, 1)]
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "content": "\n".join(lines),
        }
