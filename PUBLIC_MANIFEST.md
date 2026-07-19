# Public export manifest

This repository is an allowlist clean-room export derived from the project's
internal edition at its **v1.0 Live Validated** release line (post-release
hardening included). It was created as a fresh tree with fresh Git history;
no internal history, remotes, or metadata were carried over.

## Included categories

- Complete product source (`src/drawingmachine`): domain, application, ports,
  adapters, protocol, service, CLI, strict config, install/bootstrap logic,
  packaged resources (templates and placeholders only).
- Complete offline test suite: unit, integration, CLI, architecture,
  packaging, regression, and E2E tests with deterministic fakes and golden
  fixtures; coverage/lint/type/import gate tooling and the CI workflow.
- Current operating documentation (`docs/current/`) and the two public
  reports (refactor v0.9, live validation v1.0).
- Public governance files: README, LICENSE (MIT), CONTRIBUTING, SECURITY.

## Excluded categories

- Internal Git history and all internal review, planning, controller, and
  process records.
- Internal reports, live-validation raw evidence, and deployment runbooks.
- Historical stage-1 documents and early project-definition files.
- Personal identities, account names, numeric identities, home paths, device
  serial identities, endpoints, and machine calibration values.
- A small number of tests whose only purpose was to pin the internal
  repository's shape or internal documents.
- The internal stage-1 → stage-2 migration ledger and its replacement-proof
  test apparatus (an inventory of internal historical filenames); the CI
  command battery remains pinned by the retained architecture tests.
- One third-party trademark image previously used as a local demo input.

## Substituted content

- The example machine profile
  (`src/drawingmachine/resources/install/config/machines/default.toml.example`)
  carries illustrative placeholder geometry/feeds that intentionally differ
  from any real machine; the install-manifest digest chain was regenerated
  accordingly.
- One documentation-guard test assertion was generalized from a specific
  value to a pattern.
- CI workflow triggers and their architecture-test pin now reference this
  repository's `main` branch instead of internal branch names.
- Two stale documentation sentences were corrected to match the shipped
  migration set (schema 0005), found by independent pre-publication review.
- Agent guidance in `AGENTS.md` references internal evidence generically
  instead of linking internal files.

## Residual disclosure

- The fake-hardware test canon (fixture geometry used by Fake FluidNC tests
  and golden artifacts, including the `stage1_defaults` build profile in
  product code) is a historical product constant set. It is not claimed to
  match or not match any real machine.
- The OpenClaw agent identifiers (`cnc-drawable-*`) are the product's own
  shipped contract names.
- Provider defaults (localhost ComfyUI endpoint/port, model family name) are
  upstream-standard public defaults.

## Verification

The public tree independently passes, from its own checkout with no external
resources: full test suite, wheel/sdist build, installed-wheel checks, branch
coverage floors (90% overall, 100% safety modules), strict mypy, ruff and
format checks, and import-architecture contracts. The same battery runs in CI.
