# Drawingmachine repository agent guidance

## Current operating authority

The following eight documents are the sole current operating authority. Read the
architecture first and keep changes inside the relevant documented boundary:

- [Current architecture](docs/current/architecture.md) — first read and system ownership.
- [Installation](docs/current/installation.md) — fixed wheel layout and three principals.
- [Configuration](docs/current/configuration.md) — strict TOML and digest authority.
- [CLI reference](docs/current/cli-reference.md) — complete installed command surface.
- [OpenClaw operation](docs/current/openclaw-operation.md) — command-only operation.
- [Machine safety](docs/current/machine-safety.md) — approvals and motion milestones.
- [Recovery](docs/current/recovery.md) — staging, service, and machine recovery.
- [Development](docs/current/development.md) — offline tests and no-live rules.

The current documents describe the installed Stage 2 shape. They do not claim live
deployment or authorize changing external state.

## Repository work

- Keep production code under `src/drawingmachine` and preserve the documented inward
  dependency direction, closed schemas, ownership boundaries, and typed hardware surface.
- Add the smallest focused test that proves a change, then run the relevant focused and
  complete offline gates described by the development guide.
- Validate package behavior from a fresh installed wheel with `--no-index --no-deps` and
  temporary XDG roots; do not treat checkout imports as installation evidence.
- Keep fixtures deterministic and use fake provider and serial transports. Never put a
  secret, personal home, numeric identity, runtime endpoint, serial device, or approval
  value in documentation or fixtures.
- Treat current documents as operating contracts and Stage 1 files as byte-preserved
  history. Change either set only in a task that explicitly authorizes that scope.

## Authorization boundary

- Do not use legacy `cnc`, `drawable_path`, or public repository scripts.
- Do not use repo artifact or config JSON roots.
- Do not use direct socket or serial access.
- Do not run OpenClaw self-install, sync, or local writer operations.
- Do not perform live deployment or cutover.
- Do not perform account, group, ACL, user-unit, raw hardware, or bridge operation.

Repository work is offline. Account and permission changes, user-unit operations, real
socket probes, OpenClaw registry or managed-root changes, provider access, serial access,
motion, and cutover require a later, separately authorized task.

## Implementation evidence

Implementation history, review records, and live-validation evidence live in the
project's internal edition and are not part of this public repository. The public
validation reports under `docs/` summarize what was validated and under which
limitations.
