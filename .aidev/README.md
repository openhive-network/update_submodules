# Automated contributions

This directory configures the automated development platform that contributes to this
repository. This note says which branches it uses here and how its work reaches the
project's main line.

## Branches

- `aidev/integration` — the integration branch. Automated work lands here and nowhere else.
- `develop` — the hand-off branch: the project's development line, which receives
  `aidev/integration` by merge request.
- `ai/*` — one branch per task, created and deleted by the platform. Do not commit to them.
- `session/*` — a session's accumulated work, landed on `aidev/integration` as a batch.

## How work reaches the main line

Task and session branches land on `aidev/integration` after the platform's own
verification; they never reach `develop` directly. People bring that work into `develop`
by merging `aidev/integration` into it through a merge request, which `aidev handoff`
opens and maintainers review; nothing reaches `develop` from the platform any other way.
From `develop` it reaches `master` by this project's usual release process, which the
platform takes no part in.

Whether this project's CI pipelines skip `ai/*` and `session/*` branches is decided by the
project's CI rules; maintainers record that decision here.

## Suites run before work lands

Declared in `.aidev/project.yaml` under `verification`. The project has one small pytest
suite and no CI, so most slots share it:

- `quick`, `full`, `baseline`, `canary`, `coverage` — `pytest tests -q -p no:cacheprovider`
- `static` — `pytest tests/test_static.py -q -p no:cacheprovider`: byte-compiles the
  top-level scripts, so a syntax error in any of them fails it
