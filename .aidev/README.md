# Automated contributions

This directory configures the automated development platform that contributes to this
repository. This note says which branches it uses here and how its work reaches the
project's main line.

## Branches

- `develop` — the integration branch. Automated work lands here and nowhere else.
- `ai/*` — one branch per task, created and deleted by the platform. Do not commit to them.
- `session/*` — a session's accumulated work, landed on the integration branch as a batch.

## How work reaches the main line

Task and session branches never reach `develop` through merge requests; the platform
lands them after its own verification. If `develop` is not this project's main line,
it reaches the main line by this project's usual process, merge requests included — the
platform never merges into any other branch.

Whether this project's CI pipelines skip `ai/*` and `session/*` branches is decided by the
project's CI rules; maintainers record that decision here.

## Suites run before work lands

Declared in `.aidev/project.yaml` under `verification`:

- `quick` — `pytest` tests -q -p no:cacheprovider
- `full` — `pytest` tests -q -p no:cacheprovider
- `baseline` — `pytest` tests -q -p no:cacheprovider
- `canary` — `pytest` tests -q -p no:cacheprovider
- `static` — `pytest` tests -q -p no:cacheprovider
- `coverage` — `pytest` tests -q -p no:cacheprovider
- `system` — `pytest` tests -q -p no:cacheprovider
