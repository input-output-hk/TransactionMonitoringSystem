"""XSS regression guards for the embedded fallback dashboard template.

The template renders API data via innerHTML; ``a.source`` was interpolated
raw (review finding: stored XSS via the bulk-import source_label), and the
first guard regex only saw interpolations literally starting with ``a.`` /
``tx.``, so API values aliased through locals (``${fee}``, ``${when}``) and
expressions containing ``}`` were invisible to it. The scanner below walks
the RENDERED page (single-brace JS, no f-string artifacts) and extracts
EVERY ``${...}`` interpolation, nested ones included, with a brace-matching
parser. Each one must be fully esc()-wrapped or sit in an explicit,
hand-reviewed allowlist; the allowlist is matched exactly so a stale entry
fails the test and forces a re-review.

A second class of sink is inline event handlers: an onclick body is a
JS-string context that the HTML parser entity-decodes BEFORE the JS engine
parses it, so esc() cannot protect it (&#39;);evil()// decodes back into a
breakout). The template therefore uses data-* attributes + addEventListener
only, and a guard here pins that.

Finally, an end-to-end stored-XSS regression drives a live payload through
the real bulk-import API (the path behind the original finding), reads it
back through the JSON endpoint the dashboard polls, and executes the
template's own renderArchive() under node to assert the emitted HTML
contains only the escaped form.
"""

import asyncio
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

# Interpolations reviewed by hand and provably safe without esc(). Keyed by
# the exact expression text (post-strip); extend only with a justification.
SAFE_INTERPOLATIONS = {
    # Date/number formatter outputs: digits and locale punctuation, no markup.
    "new Date(tx.observedAt).toLocaleTimeString()": "Date formatter output, never raw API text",
    "(v * 100).toFixed(0)": "numeric formatting",
    "score.toFixed(1)": "numeric formatting",
    "a.max_score.toFixed(0)": "numeric formatting",
    # Lookups into hardcoded label tables; the miss path is esc()-wrapped.
    "SUB_SCORE_LABELS[k] || esc(k)": "static label table, esc()-wrapped fallback",
    "CLASS_LABELS[cls] || esc(cls)": "static label table, esc()-wrapped fallback",
    # Pure-literal expressions.
    "v > 0.7 ? '#ff6d00' : '#888'": "ternary over two string literals",
    "bandOf(score)": "returns one of four hardcoded band names",
    # Conditional HTML wrappers: the only dynamic piece is the NESTED
    # interpolation, which the scanner extracts and checks on its own.
    'tx.block.height ? `<span class="tx-fee">Block '
    "${esc(tx.block.height)}</span>` : ''": "static markup; nested ${esc(...)} checked separately",
    'explain ? `<div class="risk-sub" style="margin-top:2px">'
    "${explain}</div>` : ''": "static markup; nested ${explain} checked separately",
    # Locals holding HTML assembled above from esc()-wrapped / allowlisted
    # pieces only (classHtml, explain) or esc()-wrapped at assignment
    # (noteHtml: `${esc(a.note)}`, pinned by test_note_escaped_at_assignment).
    "classHtml": "built from CLASS_LABELS/esc()/bandOf/toFixed pieces",
    "explain": "explainSubScores output: label table / esc(k) / toFixed",
    "noteHtml": "esc()-wrapped at assignment",
    # fetch-URL sinks (never innerHTML): numeric/enum UI state sourced from
    # the static filter buttons, or URL-encoded with the correct encoder.
    "activeMinScore": "parseFloat of static data-band attributes; URL sink",
    "activeSort": "enum from static data-sort attributes; URL sink",
    "encodeURIComponent(txHash)": "URL component sink, correct encoder",
    "encodeURIComponent(network)": "URL component sink, correct encoder",
}


@pytest.fixture
def page() -> str:
    """The rendered dashboard HTML (f-string already evaluated, so JS
    interpolations appear with single braces exactly as the browser sees
    them)."""
    from app.routers import ui

    return asyncio.run(ui.root()).body.decode()


def extract_interpolations(page: str) -> list:
    """Every ``${...}`` expression in the page, nested ones included.

    Brace-matching instead of a regex: expressions legitimately contain
    ``}`` (object literals, nested template literals), which a ``[^}]*``
    pattern silently truncates; truncation is how the previous guard
    missed sinks.
    """
    found = []
    i = 0
    while True:
        start = page.find("${", i)
        if start == -1:
            return found
        depth = 0
        j = start + 2
        while j < len(page):
            ch = page[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        assert j < len(page), f"unterminated ${{...}} at offset {start}"
        found.append(page[start + 2 : j].strip())
        # Resume INSIDE the expression so nested ${...} are extracted too.
        i = start + 2


def is_fully_esc_wrapped(expr: str) -> bool:
    """True only when the WHOLE expression is one esc(...) call: a mere
    'contains esc(' test would wave through ``esc(a.x) + a.raw``."""
    if not (expr.startswith("esc(") and expr.endswith(")")):
        return False
    depth = 0
    for pos, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # The paren opened by esc( must be the one closing at the end.
                return pos == len(expr) - 1
    return False


class TestEscHelper:
    def test_helper_defined_once(self, page):
        assert page.count("const esc = (v)") == 1

    def test_helper_escapes_quotes_for_attribute_contexts(self, page):
        # data-* attribute interpolations need quote escaping, not just <>&.
        assert "&quot;" in page and "&#39;" in page


class TestNoInlineEventHandlers:
    def test_no_on_attribute_sinks(self, page):
        # Inline handler bodies are JS-string sinks that esc() cannot
        # protect (the HTML parser entity-decodes the attribute before the
        # JS engine parses it). Behavior must ride addEventListener +
        # data-* instead. Matches on*="..." attributes; JS property style
        # (ws.onclose = ...) is spaced and does not trigger.
        assert re.findall(r"\son[a-z]+=\"", page) == []

    def test_row_buttons_use_data_attributes_and_delegation(self, page):
        assert 'data-tx="${esc(tx.txId)}"' in page
        assert 'data-tx="${esc(a.tx_hash)}"' in page
        assert 'data-network="${esc(a.network)}"' in page
        assert "document.addEventListener" in page


class TestNoRawInterpolations:
    def test_source_field_is_escaped(self, page):
        assert "esc(a.source || 'local')" in page
        assert "${a.source || 'local'}" not in page

    def test_note_escaped_at_assignment(self, page):
        # noteHtml is allowlisted as a local: the esc() wrap lives at its
        # assignment, so pin that wrap here.
        assert "${esc(a.note)}" in page

    def test_legacy_inline_escape_idiom_removed(self, page):
        assert "replace(/[<>&]/g" not in page

    def test_every_interpolation_is_escaped_or_allowlisted(self, page):
        found = extract_interpolations(page)
        assert found, "scanner found no interpolations; template moved?"
        offenders = [
            expr
            for expr in found
            if not is_fully_esc_wrapped(expr) and expr not in SAFE_INTERPOLATIONS
        ]
        assert offenders == [], f"unescaped template interpolations: {offenders}"
        # Exact-match the allowlist in the other direction too: an entry
        # that no longer appears is stale and must be re-reviewed, not
        # silently kept as a future bypass.
        stale = sorted(set(SAFE_INTERPOLATIONS) - set(found))
        assert stale == [], f"allowlist entries not present in template: {stale}"


# ---------------------------------------------------------------------------
# End-to-end stored-XSS regression: API -> JSON -> the template's own JS.
# ---------------------------------------------------------------------------

# Breakout payload covering both sink classes: HTML element injection and
# the quote/paren sequence that escapes a JS string context.
XSS_PAYLOAD = "<img src=x onerror=alert(1)>'\");"
VALID_HASH = "c" * 64


def esc_reference(value: str) -> str:
    """Python mirror of the template's esc() (same map, & first)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@pytest.fixture
def archive_client(monkeypatch):
    """TestClient with the archive ClickHouse layer and the fail-closed
    audit writes faked in memory, mirroring tests/api/test_archive.py."""
    from app.auth import api_key
    from app.db import archive_queries, postgres
    from app.main import app

    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)

    async def fake_insert_audit(**kwargs):
        return 1

    async def fake_update_audit(audit_id, outcome):
        return None

    monkeypatch.setattr(postgres, "insert_audit_log", fake_insert_audit)
    monkeypatch.setattr(postgres, "update_audit_log_details", fake_update_audit)

    rows = {}

    async def fake_bulk_insert(entries, source_label):
        tag = f"{archive_queries.IMPORT_SOURCE_PREFIX}{source_label}"
        for e in entries:
            rows[(e["network"], e["tx_hash"])] = {
                "network": e["network"],
                "tx_hash": e["tx_hash"],
                "note": e["note"],
                "archived_by": e["archived_by"],
                "archived_at": datetime.now(UTC).replace(tzinfo=None),
                "source": tag,
            }
        return {"inserted": len(entries), "skipped": 0}

    async def fake_list(network, date_from=None, date_to=None, limit=100, offset=0):
        return [
            dict(r, max_score=None, max_class=None, risk_band=None, analyzed_at=None)
            for k, r in rows.items()
            if k[0] == network
        ]

    async def fake_count(network, date_from=None, date_to=None):
        return sum(1 for k in rows if k[0] == network)

    monkeypatch.setattr(archive_queries, "archive_bulk_insert_async", fake_bulk_insert)
    monkeypatch.setattr(archive_queries, "archive_list_async", fake_list)
    monkeypatch.setattr(archive_queries, "archive_count_async", fake_count)
    return TestClient(app)


def _extract_js(page: str, anchor: str) -> str:
    """Slice one top-level construct of the template's script out of the
    rendered page, from ``anchor`` through its closing brace/semicolon."""
    start = page.index(anchor)
    if anchor.startswith("const"):
        # The esc arrow-function statement cannot be cut at the first ';'
        # (its entity strings like '&amp;' contain semicolons); it ends
        # uniquely with the map indexing '[c]);'.
        end_marker = "[c]);"
        return page[start : page.index(end_marker, start) + len(end_marker)]
    # Function declaration: brace-match from its opening '{'.
    depth = 0
    j = page.index("{", start)
    for pos in range(j, len(page)):
        if page[pos] == "{":
            depth += 1
        elif page[pos] == "}":
            depth -= 1
            if depth == 0:
                return page[start : pos + 1]
    raise AssertionError(f"unbalanced braces after {anchor!r}")


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to execute the template's renderer",
)
def test_stored_xss_payload_is_escaped_end_to_end(archive_client, page):
    """The original finding's full path: a hostile bulk import (note and
    source_label both carry the payload) -> /api/archive JSON read by the
    dashboard -> the template's actual renderArchive() executed under node
    with a stub DOM. The emitted innerHTML must contain only the
    entity-escaped form, never the raw payload."""
    r = archive_client.post(
        "/api/archive/bulk",
        json={
            "source_label": XSS_PAYLOAD,
            "entries": [
                {
                    "network": "preprod",
                    "tx_hash": VALID_HASH,
                    "note": XSS_PAYLOAD,
                    "archived_by": "mallory",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text

    r = archive_client.get("/api/archive?network=preprod")
    assert r.status_code == 200
    items = r.json()["data"]
    # The API layer stores and returns the payload verbatim (JSON is a safe
    # transport); the escaping duty sits entirely on the renderer.
    assert items[0]["note"] == XSS_PAYLOAD

    script = "\n".join(
        [
            # Stub just enough DOM for renderArchive: an innerHTML sink and the
            # counter badge.
            "const __stubs = { archivePanel: { innerHTML: '' },"
            " archiveCount: { textContent: '' } };",
            "const document = { getElementById: (id) => __stubs[id] };",
            _extract_js(page, "const esc = (v)"),
            _extract_js(page, "function renderArchive(items)"),
            "renderArchive(JSON.parse(require('fs').readFileSync(0, 'utf8')));",
            "console.log(__stubs.archivePanel.innerHTML);",
        ]
    )
    proc = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(items),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    rendered = proc.stdout

    assert XSS_PAYLOAD not in rendered
    assert "<img" not in rendered
    # Both stored fields (note and import source label) come out escaped.
    assert f'"{esc_reference(XSS_PAYLOAD)}"' in rendered  # note
    assert f"import:{esc_reference(XSS_PAYLOAD)}" in rendered  # source
