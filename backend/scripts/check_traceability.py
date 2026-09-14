"""Fail the build when a test cited in the traceability matrix stops existing.

docs/TRACEABILITY.md maps each capability to the test that proves it, and
invites the reader to run any single row. That invitation is what makes the page
evidence rather than narrative, and it is also what makes the page decay: rename
or delete a test and the row silently becomes a claim about something that is no
longer there.

The check is static rather than a pytest run, so it needs neither database
containers nor the clustering module's separate environment, and it verifies the
host tree and the sidecar tree the same way.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

DOC = Path("docs/TRACEABILITY.md")

# A node id as pytest accepts it: a path to a test file, then the test, with an
# optional class between them. Only matched inside backticks, so prose that
# happens to contain a "::" is not mistaken for a reference.
NODE_ID = re.compile(r"`((?:[\w./-]+)/test_[\w]+\.py)::([\w:]+)`")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def defined_names(path: Path) -> set[str]:
    """Every ``test`` and ``Class::test`` name defined in one test module.

    Parametrised tests are stored under their bare function name: the matrix
    cites unparametrised ids, and expanding ``pytest.mark.parametrize`` values
    statically would add a lot of machinery for a case the page avoids.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(f"{node.name}::{child.name}")
                    # Also accept the bare name: a row may cite a method without
                    # naming its class when the class adds nothing for a reader.
                    names.add(child.name)
    return names


def main(doc_path: Path | None = None) -> int:
    """``doc_path`` overrides the matrix location, which the tests use to point
    the check at a synthetic page: a guard is only worth having if it can be
    shown to fail."""
    root = repo_root()
    doc = doc_path or (root / DOC)
    if not doc.is_file():
        print(f"error: {doc} not found", file=sys.stderr)
        return 1

    text = doc.read_text(encoding="utf-8")
    references = NODE_ID.findall(text)
    if not references:
        # The regex silently matching nothing would turn this check into a
        # rubber stamp, which is worse than not having it.
        print(
            f"error: no test references found in {doc}; the check would pass vacuously.",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    cache: dict[Path, set[str]] = {}
    for rel, target in references:
        path = root / rel
        if not path.is_file():
            errors.append(f"{rel}: file does not exist (cited as `{rel}::{target}`)")
            continue
        if path not in cache:
            cache[path] = defined_names(path)
        if target not in cache[path]:
            errors.append(f"{rel}: no test `{target}`")

    if errors:
        print(f"{doc} cites tests that no longer exist:\n", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"  - {error}", file=sys.stderr)
        print(
            f"\nEach row of {doc} must name a test a reader can run. Update the row to the "
            f"test\nthat proves the capability now, or remove the capability if it went away.",
            file=sys.stderr,
        )
        return 1

    print(f"Traceability intact: {len(references)} cited tests, all resolving")
    return 0


if __name__ == "__main__":
    sys.exit(main())
