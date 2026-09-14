"""Fail the build when a published test count drifts from reality.

Two documents publish the test inventory as a table: docs/TESTING.md ("Test
tiers at a glance") and docs/REPOSITORY-MAP.md ("Test inventory"). Both invite
the reader to re-run the collection commands and check, so a stale figure is
not cosmetic: it is a checkable false claim in the two documents an external
reviewer opens first.

A figure kept in step by hand drifts on the first commit that adds a test, and
the drift is invisible in a diff that touches neither document. So the check is
mechanical rather than a review step.

It runs in two modes, and CI uses both:

- ``--observed LOCATION=COUNT`` asserts that both documents state COUNT for the
  tier identified by LOCATION. Each CI job passes only the tiers it already has
  an environment for, so no job pays to set up another tier's toolchain.
- ``--check-totals`` asserts that each document's stated total equals the sum of
  its own tier rows, and that the two documents agree with each other. This is
  pure text arithmetic, so it needs no test environment at all.

Together the modes close the loop: a row cannot disagree with reality, and a
total cannot disagree with its rows.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The two documents that publish the inventory, relative to the repository root.
TESTING_DOC = Path("docs/TESTING.md")
REPO_MAP_DOC = Path("docs/REPOSITORY-MAP.md")
DOCS = (TESTING_DOC, REPO_MAP_DOC)

# Tier locations exactly as both tables spell them, inside backticks. These are
# the join key between the two documents and the CI observations, so they must
# match the tables character for character.
BACKEND_HERMETIC = "backend/tests/"
RECALL_GATE = "backend/tests/analysis/"
BACKEND_LIVE_DB = "backend/tests/live_db/"
SIDECAR_LIVE_DB = "services/clustering/backend/tests/live_db/"
PERF_TIER = "backend/tests/perf/"
SIDECAR = "services/clustering/backend/tests/"
FRONTEND = "frontend/src/**/*.test.{ts,tsx}"
E2E = "frontend/e2e/"

TIER_LOCATIONS = (
    BACKEND_HERMETIC,
    RECALL_GATE,
    BACKEND_LIVE_DB,
    SIDECAR_LIVE_DB,
    PERF_TIER,
    SIDECAR,
    FRONTEND,
    E2E,
)

# The recall gate is a subset of the backend hermetic suite, not a seventh tier,
# so it is published as a row but excluded from the total. Both documents say so
# in prose; this is that statement made executable.
SUBSET_LOCATIONS = frozenset({RECALL_GATE})

# A backticked span inside a Markdown table cell.
BACKTICKED = re.compile(r"`([^`]+)`")
# A cell whose text begins with an integer, thousands separators allowed. The
# recall-gate cell carries a trailing "(subset of the above)", hence the leading
# anchor rather than a full-cell match.
LEADING_INT = re.compile(r"^(\d[\d,]*)\b")
# The stated total: "N automated tests across six tiers" in one document, "That
# is N tests across the six independent tiers" in the other. The
# "across" is load-bearing rather than decorative: without it the pattern also
# matches the "97 tests" cell in REPOSITORY-MAP's alerting table, which appears
# earlier in the file than the inventory section. Requiring a leading digit is
# likewise deliberate, since a bare thousands separator would otherwise match
# prose such as "API client, tests".
STATED_TOTAL = re.compile(r"(\d[\d,]*)\s+(?:automated\s+)?tests\s+across\b")


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def repo_root() -> Path:
    """Resolve the repository root from this file's location.

    The script is invoked from several working directories (the repository root
    in the lint job, ``backend/`` in the test jobs), so paths are anchored to
    the file rather than to the caller's cwd.
    """
    return Path(__file__).resolve().parents[2]


def parse_tier_counts(doc_text: str) -> dict[str, int]:
    """Extract {tier location: published count} from a document's table rows."""
    counts: dict[str, int] = {}
    for line in doc_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = line.split("|")
        located = [
            token for cell in cells for token in BACKTICKED.findall(cell) if token in TIER_LOCATIONS
        ]
        if len(located) != 1:
            continue
        for cell in cells:
            match = LEADING_INT.match(cell.strip())
            if match:
                counts[located[0]] = _to_int(match.group(1))
                break
    return counts


def parse_stated_total(doc_text: str) -> int | None:
    match = STATED_TOTAL.search(doc_text)
    return _to_int(match.group(1)) if match else None


def check_observed(observed: dict[str, int], docs: dict[Path, str]) -> list[str]:
    """Compare measured tier counts against what each document publishes."""
    errors: list[str] = []
    for location, measured in observed.items():
        if location not in TIER_LOCATIONS:
            errors.append(
                f"unknown tier location {location!r}; expected one of {', '.join(TIER_LOCATIONS)}"
            )
            continue
        for path, text in docs.items():
            published = parse_tier_counts(text).get(location)
            if published is None:
                errors.append(f"{path}: no table row found for tier `{location}`")
            elif published != measured:
                errors.append(
                    f"{path}: tier `{location}` publishes {published} "
                    f"but collection reports {measured}"
                )
    return errors


def check_totals(docs: dict[Path, str]) -> list[str]:
    """Verify each stated total against its own rows, and the docs against each other."""
    errors: list[str] = []
    totals: dict[Path, int] = {}
    for path, text in docs.items():
        counts = parse_tier_counts(text)
        missing = [loc for loc in TIER_LOCATIONS if loc not in counts]
        if missing:
            errors.append(f"{path}: table is missing rows for {', '.join(missing)}")
            continue
        row_sum = sum(n for loc, n in counts.items() if loc not in SUBSET_LOCATIONS)
        stated = parse_stated_total(text)
        if stated is None:
            errors.append(f"{path}: no stated total found")
            continue
        totals[path] = stated
        if stated != row_sum:
            errors.append(
                f"{path}: states {stated} tests but its own rows sum to {row_sum} "
                f"(the {RECALL_GATE} row is a subset and is excluded)"
            )
    if len(set(totals.values())) > 1:
        rendered = ", ".join(f"{path} says {n}" for path, n in totals.items())
        errors.append(f"the two documents disagree on the total: {rendered}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observed",
        action="append",
        default=[],
        metavar="LOCATION=COUNT",
        help="a measured tier count to assert against both documents; repeatable",
    )
    parser.add_argument(
        "--check-totals",
        action="store_true",
        help="assert each document's stated total matches its own rows",
    )
    args = parser.parse_args()

    if not args.observed and not args.check_totals:
        parser.error("nothing to do: pass --observed and/or --check-totals")

    root = repo_root()
    docs: dict[Path, str] = {}
    for rel in DOCS:
        path = root / rel
        if not path.is_file():
            print(f"error: {rel} not found under {root}", file=sys.stderr)
            return 1
        docs[rel] = path.read_text(encoding="utf-8")

    observed: dict[str, int] = {}
    for item in args.observed:
        location, _, raw = item.partition("=")
        if not raw.strip().isdigit():
            print(
                f"error: --observed expects LOCATION=COUNT, got {item!r}",
                file=sys.stderr,
            )
            return 1
        observed[location] = int(raw)

    errors = check_observed(observed, docs)
    if args.check_totals:
        errors += check_totals(docs)

    if errors:
        print("Published test counts have drifted:\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "\nRe-measure with the commands in docs/REPOSITORY-MAP.md "
            '("Measuring this yourself") and update both tables and their totals.',
            file=sys.stderr,
        )
        return 1

    checked = ", ".join(f"`{loc}`={n}" for loc, n in observed.items())
    if checked:
        print(f"Doc counts agree for {checked}")
    if args.check_totals:
        print("Stated totals agree with their rows, and with each other")
    return 0


if __name__ == "__main__":
    sys.exit(main())
