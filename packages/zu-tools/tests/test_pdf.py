"""pdf_extract — the §9.5 non-executing PDF parser, proved offline.

The point of these tests is the §9.5 invariant made executable: a PDF that
carries embedded JavaScript is PARSED for text/structure, the active content is
REPORTED, and the JS is NEVER run (``executed == False``). pypdf has no JS engine,
so there is nothing to run — but the test pins the contract so a future change
that swaps in a renderer would fail loudly.

Every fixture PDF is built IN-TEST with pypdf's writer: no network, no external
file, deterministic, $0.
"""

from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("pypdf")

from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import (  # noqa: E402
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from zu_tools.pdf import _MAX_PDF_BYTES, PdfExtract  # noqa: E402

_KNOWN_TEXT = "HELLO_ZU_PDF"


def _page_with_text(writer: PdfWriter, text: str) -> None:
    """Add a page that draws ``text`` via an explicit content stream — so
    ``extract_text`` has something deterministic to recover (no fonts shipped by
    add_blank_page)."""
    page = writer.add_blank_page(width=300, height=300)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 24 Tf 50 150 Td (" + text.encode("latin-1") + b") Tj ET")
    content_ref = writer._add_object(content)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Contents")] = content_ref
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )


def _malicious_pdf_b64() -> str:
    """A 1-page PDF with known text AND an embedded JavaScript action."""
    w = PdfWriter()
    _page_with_text(w, _KNOWN_TEXT)
    # The attacker's primitive: embedded JS. pypdf stores it under /Names/JavaScript.
    w.add_js("app.alert('pwned')")
    w.add_metadata({"/Title": "Invoice", "/Author": "Mallory"})
    buf = io.BytesIO()
    w.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _benign_pdf_b64() -> str:
    """A 1-page PDF with text and NO active content."""
    w = PdfWriter()
    _page_with_text(w, _KNOWN_TEXT)
    buf = io.BytesIO()
    w.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --- (a) text + structure ---------------------------------------------------


async def test_extracts_text_and_page_count() -> None:
    tool = PdfExtract()
    out = await tool(None, pdf_b64=_malicious_pdf_b64())
    assert out["page_count"] == 1
    assert _KNOWN_TEXT in out["text"]
    assert _KNOWN_TEXT in out["pages"][0]
    # Typed zu_core.content output: the extracted text as a Text part.
    assert out["content"][0] == {"kind": "text", "text": out["text"]}


async def test_reads_metadata() -> None:
    tool = PdfExtract()
    out = await tool(None, pdf_b64=_malicious_pdf_b64())
    assert out["metadata"]["title"] == "Invoice"
    assert out["metadata"]["author"] == "Mallory"


# --- (b) the §9.5 proof: JS is SEEN but NOT RUN -----------------------------


async def test_reports_embedded_js_but_does_not_execute_it() -> None:
    tool = PdfExtract()
    out = await tool(None, pdf_b64=_malicious_pdf_b64())
    ac = out["active_content"]
    assert ac["javascript"] is True  # the JS was detected...
    assert ac["executed"] is False  # ...and deliberately NOT run
    assert out["executed"] is False  # top-level audit signal echoes it
    # the named script rode along and is surfaced by label
    assert isinstance(ac["names"], list)


# --- (d) a benign PDF reports no active content -----------------------------


async def test_benign_pdf_reports_no_js() -> None:
    tool = PdfExtract()
    out = await tool(None, pdf_b64=_benign_pdf_b64())
    ac = out["active_content"]
    assert ac["javascript"] is False
    assert ac["open_action"] is False
    assert ac["launch"] is False
    assert ac["uri"] is False
    assert ac["executed"] is False
    assert _KNOWN_TEXT in out["text"]


# --- (c) truthful envelope: fs:read declared, no network/egress -------------


def test_tool_declares_fs_read_and_no_egress() -> None:
    """The ``path`` arm reads the HOST filesystem, so the envelope must DECLARE
    CAP_FS_READ — that is what makes the audit-logged privilege truthful AND puts the
    tool on the containment floor (``_needs_containment`` exempts an empty-envelope
    tier-1 tool). Still no network and no egress (issue #43)."""
    from zu_core.ports import CAP_FS_READ

    tool = PdfExtract()
    assert CAP_FS_READ in tool.capabilities
    assert tool.egress == frozenset()
    assert tool.tier == 1


def test_nonempty_envelope_puts_pdf_extract_on_the_containment_floor(monkeypatch) -> None:
    """With ``containment='required'`` and ``ZU_SANDBOXED`` unset, a tool set that
    includes ``pdf_extract`` now raises ``ContainmentRequired`` listing it — proving
    the corrected (non-empty) envelope makes ``_needs_containment`` fire, closing the
    containment-floor bypass (issue #43)."""
    from zu_core.security import (
        SANDBOX_ENV,
        ContainmentRequired,
        _needs_containment,
        enforce_containment,
    )

    monkeypatch.delenv(SANDBOX_ENV, raising=False)  # a bare host: the floor is live
    tool = PdfExtract()
    assert _needs_containment(tool) is True
    with pytest.raises(ContainmentRequired) as ei:
        enforce_containment("required", {"pdf_extract": tool})
    assert "pdf_extract" in ei.value.tools


async def test_parses_local_bytes_with_no_network_touched(monkeypatch) -> None:
    """The tool must reach the network for nothing. Poison socket.socket so any
    network attempt during a parse raises — then prove a full parse still works."""
    import socket

    def _boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("pdf_extract must not touch the network")

    monkeypatch.setattr(socket, "socket", _boom)
    tool = PdfExtract()
    out = await tool(None, pdf_b64=_malicious_pdf_b64())
    assert _KNOWN_TEXT in out["text"]
    assert out["active_content"]["javascript"] is True


# --- input handling ---------------------------------------------------------


async def test_bad_base64_is_rejected_not_parsed() -> None:
    tool = PdfExtract()
    out = await tool(None, pdf_b64="not valid base64 !!!")
    assert out["blocked"] == "bad_base64"
    assert "page_count" not in out


async def test_oversized_pdf_is_rejected() -> None:
    tool = PdfExtract()
    huge = base64.b64encode(b"%PDF-" + b"x" * (_MAX_PDF_BYTES + 10)).decode("ascii")
    out = await tool(None, pdf_b64=huge)
    assert out["blocked"] == "oversized_pdf"


async def test_path_input_reads_a_local_pdf_inside_the_allowed_root(tmp_path, monkeypatch) -> None:
    """A path INSIDE the configured read root reads normally. The root is env-derived
    (``ZU_PDF_READ_ROOT``), so the test points it at ``tmp_path`` and reads a PDF
    there — the jail permits the in-root read (issue #43)."""
    monkeypatch.setenv("ZU_PDF_READ_ROOT", str(tmp_path))
    p = tmp_path / "doc.pdf"
    p.write_bytes(base64.b64decode(_benign_pdf_b64()))
    tool = PdfExtract()
    out = await tool(None, path=str(p))
    assert _KNOWN_TEXT in out["text"]
    assert out["active_content"]["javascript"] is False


async def test_absolute_path_outside_the_root_is_refused(tmp_path, monkeypatch) -> None:
    """An ABSOLUTE path outside the allowed root is refused with ``path_not_allowed``
    and reads NOTHING — even though the file exists and is a real PDF (issue #43). The
    refusal does not disclose existence: the same code fires for present and absent."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("ZU_PDF_READ_ROOT", str(root))
    # A real, readable PDF OUTSIDE the root.
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(base64.b64decode(_benign_pdf_b64()))
    tool = PdfExtract()
    out = await tool(None, path=str(outside))
    assert out["blocked"] == "path_not_allowed"
    assert "text" not in out and "page_count" not in out


async def test_traversal_escaping_the_root_is_refused(tmp_path, monkeypatch) -> None:
    """A ``..`` traversal that escapes the root is refused after ``resolve()``
    canonicalises it — the prefix check is on the CANONICAL path, so ``../secret.pdf``
    cannot smuggle a read out of the jail (issue #43)."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("ZU_PDF_READ_ROOT", str(root))
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(base64.b64decode(_benign_pdf_b64()))
    tool = PdfExtract()
    out = await tool(None, path="../secret.pdf")  # escapes root via ..
    assert out["blocked"] == "path_not_allowed"
    assert "text" not in out
