# Current Architecture

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

This page describes the installed Stage 2 code shape. It does not claim that accounts, permissions, services, OpenClaw, or hardware are deployed.

## Module boundaries

- `drawingmachine.domain` owns closed job, workflow, G-code, machine, FluidNC, planning, and OpenClaw models and transitions. It performs no mutable client, runtime, or external filesystem I/O and no socket, network, process, or serial I/O. Its narrow package-boundary exception reads installed OpenClaw package-resource streams to validate the immutable bundle.
- `drawingmachine.application` coordinates offline jobs, staging reconciliation, OpenClaw deployment, G-code checks, and typed machine actions through ports.
- `drawingmachine.ports` defines artifact, client-data, provider, repository, worker, OpenClaw, and typed FluidNC boundaries.
- `drawingmachine.adapters` implements secure filesystem access, SQLite schema migrations 0001 through 0005, bounded workers/providers, and the only serial adapter.
- `drawingmachine.service` owns one Unix-socket service, peer-credential authorization, job and machine coordinators, strict dispatch, and service-side data claims.
- `drawingmachine.cli` is a thin client. Except for `service run` and client-side input staging, it sends closed protocol requests and renders responses.
- `drawingmachine.config` loads strict application, machine, provider, service-access, and OpenClaw deployment policies.
- `drawingmachine.resources` carries the two user units, eight install resources, OpenClaw bundle, and closed manifests.

Imports point inward: domain does not import application, adapters, service, or CLI; application depends on domain and ports; adapters and service supply outer implementations. See [development](development.md) for enforcement.

## Ownership boundaries

The service principal alone owns SQLite, job artifacts, admitted/quarantined staging data, the canonical socket, machine session, serial adapter, and OpenClaw managed targets. Automation can connect, submit ordinary readable image/G-code inputs, read exported job artifacts, and request actions, but cannot consume motion approval. The operator can connect and consume a matching one-time approval, but has no job-data or staging authority. The three identities are kernel credentials, never JSON claims.

The service canonical socket is exposed to each client through a controlled symlink inside that client's private runtime directory. The service verifies `SO_PEERCRED`, the frozen access-policy digest, socket identity, ancestors, modes, group membership, and endpoint target before dispatch.

Client input follows `caller path -> private prepare lease -> atomic drop publication -> service claim -> private admitted data`; exports follow `service artifact -> service-owned export target -> automation read`. The public CLI path is unchanged and never becomes a protocol path field.

OpenClaw has two non-aliased roots: an admin-owned, service-readable registry and a service-owned managed tree. The registry is validated, never changed. The service is the only managed-tree writer; automation inherits read-only group access.

## Runtime and persistence

The service uses XDG config, state, data, and runtime roots under its dedicated account. SQLite migration 0004 records durable staging admissions in addition to prior job and machine authority. Newline-delimited protocol frames are closed schema/version objects and are bounded to 1 MiB. A restart reconciles durable jobs, machine recovery, staging leases, and endpoint state before accepting unsafe work; it does not authorize motion.

## Related guides

- [installation](installation.md)
- [configuration](configuration.md)
- [CLI reference](cli-reference.md)
- [machine safety](machine-safety.md)
- [recovery](recovery.md)
