# Documentation

Index of the project's documentation. For setup and day-to-day operations start with the root [README.md](../README.md) and [RUNBOOK.md](../RUNBOOK.md).

## Detection

- [TMS_DETECTION_SPEC.md](TMS_DETECTION_SPEC.md): the detection specification. Defines the nine attack classes (Token Dust, Large Value, Large Datum, Multiple Satisfaction, Front-Running, Sandwich, Circular Transfers, Fake Token, Phishing), the features extracted per transaction, the continuous 0-100 scoring framework, and the risk bands.

## Architecture

- [ARCHITECTURE.md](ARCHITECTURE.md): system architecture overview, the three async background tasks, and how the clustering module integrates.
- [C4-ARCHITECTURE.md](C4-ARCHITECTURE.md): C4 model at the system-context and container levels, rendered from [c4-context.mmd](c4-context.mmd) and [c4-container.mmd](c4-container.mmd).
- [TECHNOLOGY-DECISIONS.md](TECHNOLOGY-DECISIONS.md): Architecture Decision Records (ADRs) covering the main technology choices.

## Data flow

- [DATA-FLOW.md](DATA-FLOW.md): the runtime data flow as diagrams (chain-sync path, storage map, transaction lifecycle).
- [DATA-FLOW-EXPLAINED.md](DATA-FLOW-EXPLAINED.md): the same data flow as a plain-English walkthrough.

## Legal

- [LICENSES.md](LICENSES.md): third-party dependency and bundled-data licenses, plus the note on Ogmios (MPL-2.0, consumed as an external network service).

## Follow-ups

- [follow-ups/](follow-ups/): tracked engineering follow-up notes (deferred work recorded with its rationale).
