<!-- See CONTRIBUTING.md for the full rules. -->

## What & why

<!-- Summarize the change and the problem it solves. -->

## Detection changes (delete if none)

This PR touches detection code or `config/detection.yaml`.

- [ ] `cd backend && pytest tests/analysis/` is green (the recall gate: the
      attack-must-fire cases must all still pass).
- [ ] Any gate/threshold that was narrowed ships with a test proving the
      real-attack case it protects still scores in-band.
- [ ] No change trades recall for precision unless the recall case is shown to
      still fire (recall-first: a false positive beats a missed attack).

## Checklist

- [ ] `pytest tests/` green (backend)
- [ ] `pnpm lint && pnpm build` green (frontend, if touched)
- [ ] `uv run pytest -q` green (clustering sidecar, if touched)
- [ ] No unexplained numeric literals (tunables live in config / named constants)
- [ ] Docs updated if behavior or config changed
