# Verifying this repository

This guide is for anyone reviewing the system independently. It says where to
look and how to confirm what you find, pointing at the file that carries each
answer rather than copying it, so that nothing here drifts out of step with what
it points at.

## Where to start

| To establish | Read |
|---|---|
| What the system does, and how to run it | `README.md` |
| How it is put together | `docs/ARCHITECTURE.md`, `docs/C4-ARCHITECTURE.md` |
| How data moves through it | `docs/DATA-FLOW.md`, `docs/DATA-FLOW-EXPLAINED.md` |
| What it detects, and why each threshold holds the value it does | `docs/TMS_DETECTION_SPEC.md`, `config/detection.yaml` |
| Which capability is proved by which test, one row each | `docs/TRACEABILITY.md` |
| What is tested, at which tier, and how to run each tier | `docs/TESTING.md` |
| Where any given source file lives | `docs/REPOSITORY-MAP.md` |
| Why each technology was chosen, and what was rejected | `docs/TECHNOLOGY-DECISIONS.md` |
| How alerting is configured and operated | `docs/ALERTING.md` |
| How the optional clustering module works | `docs/CLUSTERING.md` |
| How a deployment is built and maintained | `docs/MAINNET-DEPLOYMENT.md` |
| Which refinements are identified but deferred, and on what reasoning | `docs/follow-ups/` |

## Confirming the tests

`docs/TESTING.md` holds the tier inventory and the command for each tier. Three
things about it are worth knowing before reading it.

The inventory is machine-checked rather than maintained by hand. Each CI job
re-collects the tiers it has an environment for and hands the counts to
`backend/scripts/check_doc_counts.py`, which fails the build when a published
figure disagrees, so a stale count in that table is a build failure rather than
something to take on trust. You can run the three checks yourself:

```bash
uv run python backend/scripts/check_doc_counts.py --check-totals
uv run python backend/scripts/check_recall_markers.py
uv run python backend/scripts/check_traceability.py
```

The detection suite runs first and on its own, so a regression in recall is
unambiguous instead of buried in a larger run. The design rule behind that
separation is that a missed attack costs more than a false positive. The cases
that carry that rule are marked, so they can be run as a set rather than
identified by name:

```bash
cd backend && uv run pytest tests/analysis/scorers/ -m attack_must_fire -q
```

Each one asserts that a real attack still scores at or above a named risk band,
and a CI check fails the build if any detection class stops having such a case.

Every tier runs from a clean checkout. The tiers that need databases are gated
behind an environment flag, so no result depends on services happening to be
present. `.github/workflows/ci.yml` is the authority on what runs when.

## Confirming the performance figures

`docs/PERFORMANCE.md` states what each benchmark measures, how to run it, and how
the budgets in `config/performance.yaml` were set. Those budgets are regression
tripwires and carry no availability or support undertaking, and the document says
so where it states them.

Raw benchmark output is not committed. `.github/workflows/perf.yml` uploads each
run's results as a workflow artifact, and `docs/PERFORMANCE.md` gives the command
to reproduce any published figure locally.

## What is not in the repository

The detection specification that the scoring framework implements is third-party
material and is not redistributed here. `docs/TMS_DETECTION_SPEC.md` states what
each rule does and where it was tuned away from that source; the scorers under
`backend/app/analysis/` cite the section each rule derives from; and
`config/detection.yaml` carries the value each one actually uses.

Deployment secrets are not tracked. Only the `.example` templates are, and
`docs/MAINNET-DEPLOYMENT.md` describes what each real file has to contain.
