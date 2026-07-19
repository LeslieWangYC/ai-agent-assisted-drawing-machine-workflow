# AI Agent Assisted Drawing Machine Workflow

`drawingmachine` is an installed service and CLI that turns images into
pen-plotter drawings through an agent-operable, safety-gated workflow:
image planning → deterministic G-code build → static safety checks →
typed machine state machine → FluidNC serial streaming, with per-action
operator approvals and durable job state.

This public repository contains the generic, self-contained core: it builds,
installs, and passes its complete offline test suite without any real machine,
account layout, network endpoint, or agent-platform installation.

## Validation status

- [Stage 2 refactor report (public, v0.9)](docs/stage2-refactor-report-public-v0.9.md)
  — offline code-completion evidence.
- [Stage 2 live-validation report (public, v1.0)](docs/stage2-live-validation-report-public-v1.0.md)
  — a full live campaign on real hardware concluded **ACCEPTED WITH
  LIMITATIONS**; the report lists every limitation explicitly.

An internal deployment edition of this project exists. This public repository
is derived from it by an allowlist clean-room export and is **not equivalent**:
real deployment parameters, account/permission layouts, live evidence, and
internal history are excluded by design. Live results cannot be reproduced
from this repository alone and public offline tests do not claim them.

## What is included

- Image planning, G-code build, static safety checks, and readiness gates.
- Typed machine state machine with per-action approvals, motion milestones,
  and recovery (`docs/current/machine-safety.md`, `docs/current/recovery.md`).
- Durable offline job chain: staging admission, review gates, artifact
  publication, quarantine lifecycle (SQLite-backed, crash-consistent).
- FluidNC serial adapter with a deterministic Fake FluidNC and fake provider
  for fully offline end-to-end tests.
- OpenClaw agent operation contract (command-only agent surface with digest
  pinning) as generic templates and placeholders.
- Strict TOML configuration with digest authority; systemd user-unit and
  install-manifest templates (placeholders only).
- Complete offline gates: ~8700 tests, branch-coverage floors (90% overall,
  100% on safety modules), strict mypy, ruff, import-architecture contracts.

## What is not included

- Real machine geometry/calibration, serial device identities, account names,
  UIDs/GIDs, endpoints, or any live deployment values (templates carry
  illustrative placeholders that intentionally differ from any real machine).
- Live-validation raw evidence, internal reports, review/controller records,
  and pre-export Git history.
- Any claim that installing this repository yields a validated hardware
  deployment. Live cutover on your own machine requires your own calibration,
  accounts, permission model, and acceptance process.

## Quickstart (offline)

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]' wheel setuptools
python -m pytest -q                      # complete offline suite
python -m build                          # wheel + sdist
```

Install the wheel and explore the CLI:

```bash
python -m pip install dist/drawingmachine-*.whl
drawingmachine --help
drawingmachine --output json workflow run --job-name example \
  --input-image examples/preview_inputs/sample_shapes.png --route-mode direct
drawingmachine --output json gcode check ./candidate.gcode
```

A self-authored sample input image lives at
`examples/preview_inputs/sample_shapes.png`; any small PNG works. Note the
workflow service processes jobs through its staged review gates — see the CLI
reference for the full job lifecycle.

See [docs/current/cli-reference.md](docs/current/cli-reference.md) for the
complete command surface and
[docs/current/development.md](docs/current/development.md) for the full gate
battery and the strict no-live rules that all tests obey.

## Documentation

Start with [docs/current/architecture.md](docs/current/architecture.md), then:

- [Installation layout](docs/current/installation.md) — wheel resources, three
  designed principals, XDG roots (all values templated).
- [Configuration](docs/current/configuration.md) — strict TOML and digest
  authority.
- [OpenClaw operation](docs/current/openclaw-operation.md) — command-only
  agent operation contract.
- [Machine safety](docs/current/machine-safety.md) and
  [Recovery](docs/current/recovery.md).

## Fake vs. live

Everything in this repository runs against deterministic fakes (Fake FluidNC,
fake provider, fake clocks, temporary XDG roots). Fake results are never
presented as live acceptance; the live campaign and its limitations are
summarized only in the public validation report. Tests enforce this boundary:
no network endpoint, real serial device, system service, or agent-platform
installation is ever contacted.

## License

MIT — see [LICENSE](LICENSE).
