# Current OpenClaw Operation

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

No current document claims that the packaged guidance is installed in a live OpenClaw root. Offline checks use a temporary registry and managed root.

## Command-only operation

OpenClaw agents invoke only `drawingmachine --output json ...`. They do not import `drawingmachine`, edit its database/artifacts, call a local deployment handler, or bypass the Unix service. The CLI `openclaw check` and `openclaw install` commands are socket requests; only the service contains the writer.

The installed bundle contains four managed targets and one check-only registry policy. `manifest.json` marks the fifth item `CHECK_ONLY_REGISTRY_POLICY`: `registry-policy.json` describes the required `openclaw.json` agent/workspace/tool entries but is never copied into or written over the registry.

## Strict registry preflight

Both commands require exactly one canonical absolute `--openclaw-root`. The service verifies that it equals the pinned registry root, reads a bounded strict `openclaw.json`, rejects duplicate/unknown/missing entries and tools, and proves every configured workspace resolves beneath the distinct managed root.

`openclaw check` is a zero-write preflight: it reports drift and leaves registry and managed targets unchanged. `openclaw install` repeats the same complete preflight before its first mutation, stages exact packaged bytes inside the managed tree, verifies identity/digest/mode/owner/group, and atomically replaces targets. A failed preflight writes zero targets; rollback/quarantine failures block later work instead of exposing mixed authority.

Automation cannot issue either operator deployment command. The configured operator may check/install, but the service still owns every filesystem mutation.

## Two roots

The registry root is read-only; the managed root is distinct and service-writable. Registry ancestry is admin-owned and only service-readable. Managed ancestry cannot alias the registry or contain client-writable components.

All managed directories are service-owned setgid `2750` with the automation-read group. All managed targets are service-owned `0640` and inherit that group. No operator, local user, or automation process writes the managed tree. Automation receives read-only traversal/file access after a separately authorized group deployment.

Never use chown, chgrp, sudo, or a proxy to bypass this boundary. There is no permission-window flow, no automation-owned target, and no registry-managed-root alias.

## Image and G-code inputs

An OpenClaw agent passes a private caller-readable image directly to `--input-image` or a G-code candidate directly to `gcode check`. The CLI automatically creates and disposes the staging lease; the service claims the request and exports service-owned results. No shell, copy, or dropbox step is a prerequisite. Caller XDG data is neither OpenClaw managed authority nor G-code artifact authority.

Automation may read only the service-owned export endpoint. It may not read service-private job storage, admitted/quarantine/observation staging, operator data, or the registry beyond the separately provisioned policy.

## Operational sequence

Offline verification may compare installed bundle bytes/digests, validate a temporary strict registry, prove `check` writes nothing, and run install only in the guarded temporary root. A later live action requires explicit approval for the real registry/managed roots, principals/groups/modes, policy digest, check result, install request, and any OpenClaw reload. OpenClaw deployment never authorizes provider calls or machine motion.

See [configuration](configuration.md) and [installation](installation.md).
