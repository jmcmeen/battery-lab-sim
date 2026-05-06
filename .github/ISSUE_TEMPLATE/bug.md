---
name: Bug report
about: Something isn't working the way the docs say it should.
title: "[bug] "
labels: bug
---

## Summary

<!-- One sentence: what's broken? -->

## Reproduction

<!-- Minimal steps. Ideally: a fresh `git clone`, the make targets you ran, the schedule(s) involved. -->

1.
2.
3.

## Expected behavior

<!-- What did you expect to happen? Cite the doc / invariant that suggests this. -->

## Actual behavior

<!-- What happened instead? Paste relevant log lines (ideally JSON output from `make logs SVC=<service>`). -->

```
<paste logs / error here>
```

## Environment

- OS:
- `uv --version`:
- `docker --version`:
- `docker compose version`:
- Branch / commit:
- Did `make smoke` pass before this issue?

## Anything else

<!-- Screenshots of Grafana, output of `make soak.status`, hypotheses, partial fixes attempted, etc. -->
