"""Fail the build when an attack class loses its recall guarantee.

The project's first rule is that a missed attack costs more than a false
positive. Enforcing it by convention asks a reviewer to notice that a precision
change has removed the last test proving a real attack still fires, which is a
lot to ask of a diff.

So the guarantee is mechanical. Every scorer class must have at least one test
marked ``@pytest.mark.attack_must_fire``, and every marked test must assert
against a band constant rather than a bare literal, so the mark cannot be
applied to a test that guarantees nothing.

``pytest tests/analysis/scorers/ -m attack_must_fire`` from ``backend/`` is
the runnable form of the same set.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MARKER = "attack_must_fire"

# One test module per scorer class, named after the class.
TEST_DIR = Path("backend/tests/analysis/scorers")

# `contract_anomaly` is in AttackClass but is not a per-transaction scorer: it
# is the clustering sidecar's verdict, merged into the score vector at API read
# time and deliberately absent from the write path (see the AttackClass
# docstring). It has no scorer module and therefore no must-fire test here; the
# sidecar carries its own suite.
NOT_SCORER_CLASSES = frozenset({"contract_anomaly"})

# A marked test has to pin the score against a band, not a bare number: the
# bands are the only thresholds that mean anything to a reader, and they move
# with the configuration if it is ever retuned.
BAND_ASSERTION = re.compile(r">=\s*BAND_(CRITICAL|HIGH|MODERATE)(_THRESHOLD)?\b")

# The mirror image, which a must-fire case must NOT contain: the SCORE bounded
# from above. Such a test proves something does not alert, which is a precision
# case, and it can still satisfy BAND_ASSERTION through an incidental comparison
# elsewhere in its body, so the set has to reject it explicitly or
# `pytest -m attack_must_fire` quietly stops meaning "these must reach a band".
# Anchored on `.score` deliberately: a must-fire case may legitimately bound a
# WEIGHT from above to establish its premise (that the shape cannot reach the
# band on weights alone, so a floor has to promote it).
BAND_UPPER_BOUND = re.compile(r"\.score\s*<=?\s*BAND_(CRITICAL|HIGH|MODERATE)(_THRESHOLD|_MAX)?\b")


def repo_root() -> Path:
    """Repo root from this file's location, so the check runs from anywhere."""
    return Path(__file__).resolve().parents[2]


def attack_classes(root: Path) -> list[str]:
    """Scorer class names, read from the enum rather than from a hand list.

    Adding a scorer means adding an enum member, so a new class arrives here
    without anyone remembering to update this script.
    """
    sys.path.insert(0, str(root / "backend"))
    # Imported here, not at module scope: it needs the sys.path line above.
    from app.models.transaction import AttackClass

    names = {c.value for c in AttackClass}
    unknown = NOT_SCORER_CLASSES - names
    if unknown:
        # The exclusion is stale: it would silently exclude nothing and the
        # check would then demand a test for a class that has no module.
        raise SystemExit(
            f"error: NOT_SCORER_CLASSES names {sorted(unknown)}, which are not in "
            f"AttackClass any more. Update the exclusion in {Path(__file__).name}."
        )
    return sorted(names - NOT_SCORER_CLASSES)


def marked_tests(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Names of marked tests in one module, split into pinned, unpinned, inverted.

    "Unpinned" means marked but with no band assertion, which is the failure
    mode where the marker spreads without carrying a guarantee. "Inverted" means
    marked while asserting the score stays at or below a band, which is a
    precision case wearing a recall mark.
    """
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    pinned: list[str] = []
    unpinned: list[str] = []
    inverted: list[str] = []
    for node in ast.walk(ast.parse(src)):
        # Async too: a marked async test that this missed would be invisible
        # here while still running under `pytest -m`.
        if not (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        ):
            continue
        decorators = "".join(
            "".join(lines[d.lineno - 1 : (d.end_lineno or d.lineno)]) for d in node.decorator_list
        )
        if MARKER not in decorators:
            continue
        body = "".join(lines[node.lineno - 1 : (node.end_lineno or node.lineno)])
        if BAND_UPPER_BOUND.search(body):
            inverted.append(node.name)
        elif BAND_ASSERTION.search(body):
            pinned.append(node.name)
        else:
            unpinned.append(node.name)
    return pinned, unpinned, inverted


def main() -> int:
    root = repo_root()
    errors: list[str] = []
    total = 0

    # Half one: every class still has a pinned case. Keyed on the enum, so a new
    # scorer arrives here without anyone updating this script.
    for attack_class in attack_classes(root):
        path = root / TEST_DIR / f"test_{attack_class}.py"
        if not path.is_file():
            errors.append(f"{attack_class}: no test module at {TEST_DIR}/{path.name}")
            continue
        pinned, _, _ = marked_tests(path)
        if not pinned:
            errors.append(
                f"{attack_class}: no test marked @pytest.mark.{MARKER} asserting against a "
                f"band. A real-attack case for this class can now regress silently."
            )

    # Half two: every marked case in the directory carries a real guarantee. The
    # published runnable set is the whole directory, not just the nine
    # class-named modules, so a marker added to a sibling module joins the set
    # and has to be held to the same rule.
    for path in sorted((root / TEST_DIR).glob("test_*.py")):
        pinned, unpinned, inverted = marked_tests(path)
        total += len(pinned)
        for name in unpinned:
            errors.append(
                f"{path.name}: {name} is marked @pytest.mark.{MARKER} but asserts no "
                f"band threshold, so the mark promises a guarantee it does not make."
            )
        for name in inverted:
            errors.append(
                f"{path.name}: {name} is marked @pytest.mark.{MARKER} but asserts the "
                f"score stays at or below a band. That is a precision case; drop the mark."
            )

    if errors:
        print("Recall guarantee is not intact:\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            f"\nEvery scorer class needs at least one @pytest.mark.{MARKER} test that "
            f"asserts\na real-attack case scores at or above a band constant. Run the set "
            f"with:\n  cd backend && pytest tests/analysis/scorers/ -m {MARKER}",
            file=sys.stderr,
        )
        return 1

    print(f"Recall guarantee intact: {total} marked cases across every scorer class")
    return 0


if __name__ == "__main__":
    sys.exit(main())
