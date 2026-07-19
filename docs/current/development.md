# Current Development Guide

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

Development and review operate only in repository, wheel, and temporary roots. They do not deploy the branch.

## Offline test architecture

- Unit tests cover closed domain/config/protocol behavior and filesystem primitives.
- Integration tests use temporary SQLite, Unix sockets, XDG roots, fake provider transports, and injected FluidNC sessions.
- Architecture tests freeze import directions, safety-module inventory, package resources, legacy boundaries, and no-live guards.
- Packaging tests build a clean Git-archive wheel, install with `--no-index --no-deps`, and compare installed help/resources/import provenance rather than checkout imports.
- E2E fixtures start a guarded temporary service, capture forbidden calls, close processes/sockets/descriptors, and leave no runtime residue.

Permission-oracle tests model disjoint service/automation/operator identities and exact DAC/ACL outcomes because a same-UID process cannot prove cross-UID Linux enforcement. D9 supplements that model with real temporary AF_UNIX `SO_PEERCRED` behavior and clearly labels it same-UID-only. Live cross-UID acceptance remains separate.

## Fixtures and fakes

Fixtures use placeholders, temporary absolute roots, deterministic clocks/IDs, stable tiny inputs, and explicit hostile variants. Production serial code may be imported/executed in tests only with injected `FakeSerial`; tests must prove the default pyserial factory is not constructed and no real/default device is opened. Fake FluidNC/provider behavior cannot be presented as live acceptance.

Golden Package B/C fixtures preserve input and safety behavior. Tests must not regenerate trusted goldens from the code under test during assertion, weaken strict schemas, or aggregate distinct principal/permission claims into one same-UID result.

## Coverage and gates

Run focused tests first, then the complete unit/integration/CLI/architecture/packaging/regression/E2E suite. Production changes require whole statement and branch coverage at least 90%, every file in the exhaustive safety manifest at 100% branch coverage, and all G-code safety modules at 100%. Ruff, format, strict mypy, import contracts, deterministic resource hashes, installed-wheel help/resources, Markdown links/code blocks, and secret/path scans are independent gates.

Pure prose plus a docs-only contract test does not change production authority or a shared test helper, so it does not require a new coverage artifact. It still requires the complete no-coverage suite and all documentation/packaging/no-live gates.

## No-live rule

Never contact a provider, OpenClaw installation, default service socket, system manager, network endpoint, real serial/controller, or hardware during code completion. Never create/change real accounts, groups, ACLs, linger, units, registry, managed roots, or user data. Guards reject bare, path, and shell forms before spawn/open; audit all child processes, sockets, serial factories, threads, FDs, databases/sidecars, build trees, egg-info, and coverage/runtime files after a gate.

A separately authorized live task starts from an accepted clean commit and its reviewed install/configuration digests; it is not an extension of an offline test command.

See [architecture](architecture.md) and [installation](installation.md).
