# Current Configuration

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

Packaged `.example` files are render inputs. Placeholder substitution and any deployment of rendered bytes are later cutover actions; offline tests only parse temporary copies.

## Strict TOML

All five TOML documents are closed: unknown or missing keys, wrong schema version/type, unsafe paths, duplicate principals, and unbounded values fail before service authority is created.

- `config.toml`: exactly `schema_version`, `machine_profile`, `provider_profile`, and `log_level`; profile names resolve to contained files beneath `machines/` and `providers/`.
- Machine profile: schema-1 envelope with exact `name`, `planning`, `gcode`, and `hardware` tables. Hardware is fixed to typed FluidNC serial semantics, bounded timeouts, G54, homed MPos/tolerance, approval TTL, and disjoint automation/operator principals. It cannot contain raw commands.
- Provider profile: schema-1 Local ComfyUI envelope with a contained workflow template, fixed node map, bounded polling/timeouts, and explicit live-execution guard.
- `service-access.toml`: exact service/automation/operator/connect identities, canonical socket/data roots, four controlled client endpoints, and OpenClaw policy path. The service file is `0640`; the client helper requires its rendered exact digest.
- `openclaw-deployment.toml`: exact principals/group/owner, distinct registry and managed roots, fixed `2750`/`0640` modes, and exact packaged manifest/policy digests.

Use `drawingmachine --output json config check` for offline validation only with temporary XDG roots and placeholders. This command validates only the application, machine, and provider documents; it does not validate `service-access.toml` or `openclaw-deployment.toml`, which are service-start/deployment boundaries. It does not create directories, change permissions, start a service, contact a provider, or open serial.

## Digest authority

The SHA-256 of the exact application, machine, and provider bytes is computed at load. Those application, machine, and provider byte digests enter job configuration authority and bind machine approvals, along with the frozen job revision and G-code digest.

The rendered service-access digest binds startup, client endpoint helpers, socket/access identity, and controlled endpoint policy. The OpenClaw deployment policy pins the packaged manifest and registry policy. These service-access and OpenClaw deployment-policy digests do not enter job or machine authority; they govern separate deployment/access boundaries and cannot alter motion semantics.

Current packaged OpenClaw authority digests are:

- manifest bytes: `38b32c0aaa0d88f8ddcc6d9bee430a1eacbc879edf709f9850cbf84fc61d4f4b`
- registry policy bytes: `a3ae1ae494359e76e742d1b99e1113c3a4bb9db08bfc78375ce68ed445f7fb08`

Rebuild tooling must derive these from the installed wheel; copying values from this page is not deployment approval.

## Machine authority

Planning/G-code configuration is the sole source for canvas, machine bounds, center, Z values, feeds, work coordinate, mirroring, and path mode. Hardware configuration cross-validates those values and adds transport/timing/principal facts; it does not duplicate motion commands. A real serial path and physical measurements require a separate live/hardware approval.

## Service and OpenClaw policy

Service access requires three disjoint principals, a supplemental connect group, canonical socket ownership/mode, endpoint symlinks, traversal-only access, and exact automation import/export ACL/default-ACL facts. OpenClaw requires a read-only registry, a non-aliased service-owned managed root, service-owned ancestors, automation-read membership, and exact installed resource hashes. Neither policy trusts caller XDG paths, JSON identity fields, or filesystem names alone.

See [installation](installation.md) and [OpenClaw operation](openclaw-operation.md).
