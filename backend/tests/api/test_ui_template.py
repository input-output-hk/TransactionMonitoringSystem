"""XSS regression guard for the embedded fallback dashboard template.

The template renders API data via innerHTML; ``a.source`` was interpolated
raw (review finding: stored XSS via the bulk-import source_label). The
template is static text, so these tests assert on its source rather than
executing JS: every ``${a.*}`` / ``${tx.*}`` interpolation must be wrapped
in the shared esc() helper, with an explicit allowlist for values computed
by safe builtins (toFixed / toLocaleString / Date).
"""

import re

import pytest

# Interpolations whose value is produced by a number/date formatter inside
# the template itself (never raw API data). Reviewed by hand; extend only
# for provably computed values.
SAFE_COMPUTED = {
    "a.max_score.toFixed(0)",
    "new Date(tx.observedAt).toLocaleTimeString()",
}


@pytest.fixture
def template() -> str:
    from app.routers import ui
    import inspect

    return inspect.getsource(ui)


class TestEscHelper:
    def test_helper_defined_once(self, template):
        assert template.count("const esc = (v)") == 1

    def test_helper_escapes_quotes_for_attribute_contexts(self, template):
        # onclick='...' interpolations need quote escaping, not just <>&.
        assert "&quot;" in template and "&#39;" in template


class TestNoRawInterpolations:
    def test_source_field_is_escaped(self, template):
        assert "esc(a.source || 'local')" in template
        assert "${{a.source || 'local'}}" not in template

    def test_legacy_inline_escape_idiom_removed(self, template):
        assert "replace(/[<>&]/g" not in template

    def test_every_api_interpolation_is_escaped_or_allowlisted(self, template):
        # The file is a Python f-string: literal JS interpolations appear
        # as ${{...}}. Find every one referencing API data (a.* / tx.*)
        # and require esc( in the expression unless allowlisted.
        raw = re.findall(r"\$\{\{((?:a|tx)\.[^}]*)\}\}", template)
        offenders = [
            expr for expr in raw
            if "esc(" not in expr and expr.strip() not in SAFE_COMPUTED
        ]
        assert offenders == [], f"unescaped template interpolations: {offenders}"
