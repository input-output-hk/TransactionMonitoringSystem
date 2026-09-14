"""Tests for the traceability-matrix guard.

The matrix invites a reader to run any single row, which is what makes it
evidence rather than narrative. The guard is what keeps that invitation true, so
it has to be shown to fail: a check that passes whatever the page says would
report traceability intact while the rows pointed at deleted tests.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_traceability.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_traceability", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load()


def _doc(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "TRACEABILITY.md"
    path.write_text(body, encoding="utf-8")
    return path


# A row that really does resolve, so the fixtures below differ from it in one
# way only. Kept as a reference to this module's own first test.
REAL = "backend/tests/test_traceability_check.py::TestGuard::test_a_resolving_reference_passes"


class TestGuard:
    def test_a_resolving_reference_passes(self, checker, tmp_path):
        assert checker.main(_doc(tmp_path, f"| cap | impl | `{REAL}` |\n")) == 0

    def test_a_missing_test_fails(self, checker, tmp_path, capsys):
        doc = _doc(
            tmp_path,
            "| cap | impl | `backend/tests/test_traceability_check.py::test_deleted_last_week` |\n",
        )
        assert checker.main(doc) == 1
        assert "no test" in capsys.readouterr().err

    def test_a_missing_file_fails(self, checker, tmp_path, capsys):
        doc = _doc(tmp_path, "| cap | impl | `backend/tests/test_gone_away.py::test_x` |\n")
        assert checker.main(doc) == 1
        assert "does not exist" in capsys.readouterr().err

    def test_a_page_with_no_references_fails(self, checker, tmp_path, capsys):
        """A silently non-matching regex would make this a rubber stamp, which
        is worse than having no check: CI would report traceability intact for a
        page that traced nothing."""
        doc = _doc(tmp_path, "# Traceability\n\nProse only, no rows yet.\n")
        assert checker.main(doc) == 1
        assert "vacuously" in capsys.readouterr().err

    def test_prose_containing_a_colon_pair_is_not_read_as_a_reference(self, checker, tmp_path):
        """The reference has to be inside backticks, so a sentence mentioning
        `a::b` in passing does not become a row to verify."""
        doc = _doc(tmp_path, f"Note that a::b is not a citation.\n\n| c | i | `{REAL}` |\n")
        # Passing is the assertion: had the prose been read as a reference, the
        # unresolvable `a::b` would have failed the page.
        assert checker.main(doc) == 0


class TestDefinedNames:
    def test_a_method_is_addressable_with_and_without_its_class(self, checker, tmp_path):
        module = tmp_path / "test_sample.py"
        module.write_text("class TestThing:\n    def test_it(self): pass\n", encoding="utf-8")
        names = checker.defined_names(module)
        assert "TestThing::test_it" in names
        assert "test_it" in names

    def test_module_level_tests_are_found(self, checker, tmp_path):
        module = tmp_path / "test_sample.py"
        module.write_text("def test_top_level(): pass\n", encoding="utf-8")
        assert "test_top_level" in checker.defined_names(module)

    def test_async_tests_are_found(self, checker, tmp_path):
        """Most of the suite is asyncio_mode=auto, so async defs are the norm
        and a guard that missed them would fail on valid rows."""
        module = tmp_path / "test_sample.py"
        module.write_text("async def test_async(): pass\n", encoding="utf-8")
        assert "test_async" in checker.defined_names(module)


class TestThisRepository:
    def test_every_cited_test_currently_resolves(self, checker):
        assert checker.main() == 0
