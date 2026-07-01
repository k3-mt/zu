"""pdf_extract — a NON-EXECUTING PDF text/structure parser (tier 1, §9.5).

§9.5 (the worked threat model) prefers a *non-executing document path*: extract
text and structure WITHOUT running the document's embedded JavaScript or actions.
A renderer that executes a malicious PDF's JS is the attack surface; a pure parser
that only reads the content streams never runs the JS — "do not give the attacker
the primitive in the first place. Prevention above containment."

pypdf is exactly such a pure parser: it decodes content streams and the object
graph, and it has **no JavaScript engine**, so a doc's ``/JS``/``/JavaScript``,
``/OpenAction``, ``/AA``, ``/Launch`` and ``/URI`` actions are *data we read*, never
code we run. This tool surfaces that active content as a report — so the agent and
the audit log SEE that the document carried it AND that it was deliberately not
executed (``"executed": false``) — while never invoking any of it.

No egress: the tool parses local bytes (base64 or a path); it never fetches a URL.
If a URL is needed, the agent composes ``http_fetch`` (SSRF-guarded + egress-
allowlisted) and passes the bytes here — keeping ``pdf_extract`` egress-free is the
least-privilege point.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zu_core.content import Text
from zu_core.ports import CAP_FS_READ

if TYPE_CHECKING:  # avoid a hard import at module top — the extra may be absent.
    from pypdf import PdfReader

# Defensive cap on the bytes handed to the parser. The ``pdf`` arg can arrive
# straight from the model/task with no fetch cap in front of it, and a hostile
# document is a CPU/memory DoS in-process. Mirror the fetch byte cap (5 MB).
_MAX_PDF_BYTES = 5_000_000

# The env var that names the ONE root the ``path`` arm may read under. Generic and
# config/env-derived — never a hardcoded site path. Unset ⇒ fall back to the run's
# working directory (``os.getcwd()``): a caller that wants a different jail exports
# ``ZU_PDF_READ_ROOT`` (e.g. the run's workspace). The read is confined to this
# root; an absolute path outside it or a ``..`` traversal that escapes it is refused
# WITHOUT disclosing whether the target exists (issue #43).
_READ_ROOT_ENV = "ZU_PDF_READ_ROOT"


def _read_root() -> Path:
    """The resolved allowed root for the ``path`` arm: ``$ZU_PDF_READ_ROOT`` when set,
    else the current working directory. ``resolve()`` canonicalises it (symlinks,
    ``..``) so the prefix check below compares two canonical paths."""
    raw = os.environ.get(_READ_ROOT_ENV) or os.getcwd()
    return Path(raw).resolve()


def _jail_path(path: str) -> Path | None:
    """Confine ``path`` to the allowed root. Returns the resolved path when it is a
    regular file WITHIN the root; returns ``None`` (refuse — do not leak existence)
    for anything else: an absolute/relative target that resolves outside the root, a
    ``..`` traversal that escapes it, or a non-file (directory, device, symlink to
    outside). ``resolve()`` canonicalises before the prefix check so ``../`` and
    symlinks cannot smuggle the read out of the jail."""
    root = _read_root()
    try:
        resolved = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    # Prefix check on the CANONICAL paths: the resolved target must be at or under
    # the root. ``is_relative_to`` is the exact "is a prefix of" test.
    if not resolved.is_relative_to(root):
        return None
    # Reject non-regular targets (directories, devices, FIFOs) — only a real file is
    # a readable PDF; ``is_file`` follows symlinks but the resolved path is already
    # inside the root, so a symlink pointing out was rejected above.
    if not resolved.is_file():
        return None
    return resolved

# The PDF action/active-content keys we DETECT (and never execute). These are the
# primitives §9.5 calls out: embedded JS, document-open and additional actions
# that can run JS, and launch/URI actions.
_JS_KEYS = ("/JS", "/JavaScript")
_OPEN_ACTION_KEY = "/OpenAction"
_AA_KEY = "/AA"  # additional-actions dict (e.g. /O document-open, /WC will-close)
_LAUNCH_KEY = "/Launch"
_URI_KEY = "/URI"

_MISSING_PYPDF = (
    "pdf_extract needs pypdf: pip install 'zu-tools[pdf]'"
)


def _load_reader(data: bytes) -> PdfReader:
    """Lazy-import pypdf and open the bytes. The import is INSIDE the call so a
    base ``zu-tools`` install (without the ``[pdf]`` extra) still imports and is
    discoverable; a clear, typed error fires only when the tool is actually used."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised via the install hint
        raise RuntimeError(_MISSING_PYPDF) from exc
    import io

    return PdfReader(io.BytesIO(data))


def _bytes_carry_js(data: bytes) -> bool:
    """A raw byte-level fallback scan for embedded-JS markers. The structured
    walk below is authoritative, but a malformed/obfuscated object graph could
    keep pypdf from surfacing a name into ``/Names``/``/OpenAction``; the raw scan
    is a belt-and-suspenders signal. It NEVER executes anything — it only greps
    the decoded bytes for the JS action keys."""
    return b"/JavaScript" in data or b"/JS" in data


def _scan_active_content(reader: PdfReader, data: bytes) -> dict[str, Any]:
    """Walk the document's object graph and REPORT active content — without ever
    invoking it. Returns the §9.5 safety signal.

    The walk is read-only: it inspects keys on the catalog, the names tree, and
    each page's annotations/actions. Nothing here dispatches an action.
    """
    names: list[str] = []
    javascript = False
    open_action = False
    additional_actions = False
    launch = False
    uri = False

    try:
        root = reader.trailer["/Root"]
    except Exception:  # noqa: BLE001 - a malformed catalog must not crash the parser
        root = None

    if root is not None:
        root_obj: Any
        try:
            root_obj = root.get_object()
        except Exception:  # noqa: BLE001
            root_obj = root

        # Document-level open action (/OpenAction) — can run JS on open. Present?
        if _OPEN_ACTION_KEY in root_obj:
            open_action = True
            if _action_is_js(root_obj.get(_OPEN_ACTION_KEY)):
                javascript = True

        # Document-level additional actions (/AA).
        if _AA_KEY in root_obj:
            additional_actions = True
            if _aa_has_js(root_obj.get(_AA_KEY)):
                javascript = True

        # The names tree (/Names/JavaScript) — where named JS scripts live. This
        # is exactly where pypdf's own ``add_js`` stores an embedded script.
        try:
            names_dict = root_obj.get("/Names")
        except Exception:  # noqa: BLE001
            names_dict = None
        if names_dict is not None:
            try:
                names_obj = names_dict.get_object()
            except Exception:  # noqa: BLE001
                names_obj = names_dict
            js_tree = _safe_get(names_obj, "/JavaScript")
            if js_tree is not None:
                javascript = True
                names.extend(_collect_name_labels(js_tree))

    # Per-page annotations / actions — links can carry /Launch or /URI actions.
    try:
        pages = list(reader.pages)
    except Exception:  # noqa: BLE001
        pages = []
    for page in pages:
        annots = _safe_get(page, "/Annots")
        if annots is None:
            continue
        try:
            annots_list = list(annots)
        except Exception:  # noqa: BLE001
            continue
        for annot in annots_list:
            try:
                a = annot.get_object()
            except Exception:  # noqa: BLE001
                continue
            action = _safe_get(a, "/A")
            if action is not None:
                subtype = _action_subtype(action)
                if subtype == _LAUNCH_KEY:
                    launch = True
                elif subtype == _URI_KEY:
                    uri = True
                if _action_is_js(action):
                    javascript = True
            # Annotation additional-actions (/AA) can also run JS.
            if _safe_get(a, _AA_KEY) is not None:
                additional_actions = True
                if _aa_has_js(_safe_get(a, _AA_KEY)):
                    javascript = True

    # Belt-and-suspenders raw scan: if the bytes carry JS markers the structured
    # walk missed (obfuscated graph), still surface it. Never executes.
    if not javascript and _bytes_carry_js(data):
        javascript = True

    return {
        "javascript": javascript,
        "open_action": open_action,
        "additional_actions": additional_actions,
        "launch": launch,
        "uri": uri,
        "names": names,
        # The §9.5 invariant, made explicit and auditable: this tool is a pure
        # parser; it REPORTS active content, it never runs it.
        "executed": False,
    }


def _safe_get(obj: Any, key: str) -> Any:
    try:
        if key in obj:
            return obj[key]
    except Exception:  # noqa: BLE001
        return None
    return None


def _action_subtype(action: Any) -> str | None:
    """The /S subtype of an action dict (e.g. /JavaScript, /Launch, /URI)."""
    try:
        a = action.get_object()
    except Exception:  # noqa: BLE001
        a = action
    s = _safe_get(a, "/S")
    return str(s) if s is not None else None


def _action_is_js(action: Any) -> bool:
    """True if an action dict is (or chains to) a JavaScript action. Read-only."""
    try:
        a = action.get_object()
    except Exception:  # noqa: BLE001
        a = action
    if _action_subtype(a) == "/JavaScript":
        return True
    for key in _JS_KEYS:
        if _safe_get(a, key) is not None:
            return True
    # An action can chain a /Next action; follow it (read-only) to report JS.
    nxt = _safe_get(a, "/Next")
    if nxt is not None:
        try:
            for sub in list(nxt) if isinstance(nxt, list) else [nxt]:
                if _action_is_js(sub):
                    return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _aa_has_js(aa: Any) -> bool:
    """Any entry in an additional-actions (/AA) dict that runs JS. Read-only."""
    try:
        a = aa.get_object()
    except Exception:  # noqa: BLE001
        return False
    try:
        values = list(a.values())
    except Exception:  # noqa: BLE001
        return False
    return any(_action_is_js(v) for v in values)


def _collect_name_labels(js_tree: Any) -> list[str]:
    """The labels (names) of the embedded scripts in a /JavaScript name tree —
    reported so the agent knows WHICH scripts rode along. The script *bodies* are
    deliberately NOT returned and NEVER executed."""
    labels: list[str] = []
    try:
        tree = js_tree.get_object()
    except Exception:  # noqa: BLE001
        tree = js_tree
    arr = _safe_get(tree, "/Names")
    if arr is None:
        return labels
    try:
        items = list(arr)
    except Exception:  # noqa: BLE001
        return labels
    # A name tree's /Names is a flat [key1, value1, key2, value2, ...] array;
    # the even indices are the string labels.
    for i in range(0, len(items), 2):
        try:
            labels.append(str(items[i]))
        except Exception:  # noqa: BLE001
            continue
    return labels


class PdfExtract:
    name = "pdf_extract"
    tier = 1  # pure CPU on bytes it is handed; no escalation needed to use it
    # The ``path`` arm reads the HOST filesystem, so the envelope must DECLARE it:
    # CAP_FS_READ. Declaring it is what makes the audit-logged privilege TRUTHFUL and
    # what puts the tool on the containment floor — ``_needs_containment`` treats an
    # empty-envelope tier-1 tool as host-safe, so an empty declaration would exempt a
    # host read from ``containment='required'`` (issue #43). Still NO CAP_NET / no
    # write / no subprocess: it never fetches a URL and never egresses what a hostile
    # doc points at (the §9.5 point). The ``path`` read itself is jailed to a
    # configured root (``_jail_path``); ``pdf_b64`` stays the egress-governed way in.
    capabilities: frozenset[str] = frozenset({CAP_FS_READ})
    egress: frozenset[str] = frozenset()
    schema = {
        "name": "pdf_extract",
        "description": (
            "Extract text + structure from a PDF WITHOUT rendering it or running "
            "its embedded JavaScript. Pass the PDF as base64 ('pdf_b64') or a local "
            "'path'. Reports any active content (JS/OpenAction/launch/URI) it found "
            "and did NOT execute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pdf_b64": {"type": "string", "description": "the PDF bytes, base64"},
                "path": {
                    "type": "string",
                    "description": (
                        "a local path to the PDF, confined to the configured read "
                        "root (ZU_PDF_READ_ROOT, else cwd); paths outside it are refused"
                    ),
                },
            },
        },
    }
    prompt_fragment = (
        "pdf_extract(pdf_b64 | path): pull TEXT + structure from a PDF as a pure "
        "parser — it does NOT render the PDF and does NOT run embedded JavaScript. "
        "It reports active content (JS/OpenAction/launch/URI) it saw but did not "
        "execute. No network: fetch with http_fetch first, then pass the bytes."
    )

    async def __call__(
        self,
        ctx: Any,
        pdf_b64: str | None = None,
        path: str | None = None,
    ) -> dict:
        # Resolve the source to bytes. Exactly one of base64 / path is expected.
        if pdf_b64 is not None and path is not None:
            return {"error": "pass pdf_b64 OR path, not both", "blocked": "ambiguous_input"}
        if pdf_b64 is not None:
            try:
                data = base64.b64decode(pdf_b64, validate=True)
            except (binascii.Error, ValueError):
                return {"error": "pdf_b64 is not valid base64", "blocked": "bad_base64"}
        elif path is not None:
            # Jail the read to the configured allowed root: realpath + prefix check.
            # A path outside the root (absolute or via ``..``) or a non-file target is
            # REFUSED without disclosing whether it exists (issue #43) — a hostile
            # model cannot read ``/etc/passwd`` or ``../../secrets`` off the host.
            jailed = _jail_path(path)
            if jailed is None:
                return {
                    "error": "path is not within the allowed read root",
                    "blocked": "path_not_allowed",
                }
            try:
                with open(jailed, "rb") as fh:
                    data = fh.read(_MAX_PDF_BYTES + 1)
            except OSError:
                # Do not echo the path or the OS error — a refusal must not leak
                # existence/permission detail about a target outside the caller's reach.
                return {"error": "path is not readable", "blocked": "unreadable_path"}
        else:
            return {"error": "pass pdf_b64 or path", "blocked": "no_input"}

        if len(data) > _MAX_PDF_BYTES:
            return {
                "error": f"pdf exceeds the {_MAX_PDF_BYTES}-byte parse limit and was rejected",
                "blocked": "oversized_pdf",
            }

        try:
            reader = _load_reader(data)
        except RuntimeError as exc:  # the missing-pypdf install hint
            return {"error": str(exc), "blocked": "missing_pypdf"}
        except Exception as exc:  # noqa: BLE001 - a malformed PDF is a tool error, not a crash
            return {"error": f"could not parse PDF: {exc}", "blocked": "unparseable_pdf"}

        # --- TEXT + STRUCTURE (read-only) -----------------------------------
        per_page: list[str] = []
        for page in reader.pages:
            try:
                per_page.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page must not lose the rest
                per_page.append("")
        full_text = "\n".join(per_page)
        page_count = len(reader.pages)

        metadata = self._read_metadata(reader)
        outline = self._read_outline(reader)

        # --- THE §9.5 SAFETY SIGNAL: detect + REPORT, never execute ----------
        active_content = _scan_active_content(reader, data)

        # Typed multimodal currency: the extracted text as a zu_core Text part.
        content = [Text(text=full_text)] if full_text else []

        return {
            "content": [c.model_dump(mode="json") for c in content],
            "text": full_text,
            "page_count": page_count,
            "pages": per_page,
            "metadata": metadata,
            "outline": outline,
            "active_content": active_content,
            # Echoed at the top level too so the audit log reads it without
            # descending into active_content: this is a non-executing parser.
            "executed": False,
        }

    def _read_metadata(self, reader: PdfReader) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            meta = reader.metadata
        except Exception:  # noqa: BLE001
            meta = None
        if meta is None:
            return out
        title = getattr(meta, "title", None)
        author = getattr(meta, "author", None)
        if title:
            out["title"] = str(title)
        if author:
            out["author"] = str(author)
        return out

    def _read_outline(self, reader: PdfReader) -> list[str]:
        """A flat list of outline (bookmark) titles, if any. Read-only."""
        labels: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, list):
                for child in node:
                    _walk(child)
                return
            title = getattr(node, "title", None) or _safe_get(node, "/Title")
            if title:
                labels.append(str(title))

        try:
            _walk(reader.outline)
        except Exception:  # noqa: BLE001
            return labels
        return labels
