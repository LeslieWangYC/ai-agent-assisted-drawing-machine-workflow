# Current Installation Shape

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

The steps below separate offline verification from later live work. Nothing in this guide authorizes creating accounts, changing permissions, starting a unit, or touching a real socket or device.

## Fixed wheel layout

Offline verification builds from a clean Git archive, installs the wheel with `--no-index --no-deps`, and proves imports/resources come from that wheel. Each of the service, automation, and operator principals has its own fixed `%h/.local/lib/drawingmachine/venv`; the service unit invokes `%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run`, while each client unit invokes its own `%h/.local/lib/drawingmachine/venv/bin/python -m drawingmachine.install.client_endpoint`. PATH lookup and repository imports are not deployment authority.

A separately authorized live cutover would install the same verified wheel into all three fixed venvs, render the packaged templates once, verify their exact bytes/modes/owners, and only then place user units.

## Rebuild order

1. **Stop both client units.** Stop the automation and operator endpoint helpers so neither can run an old wheel or policy digest.
2. **Stop the service unit.** Preserve state and recovery evidence before replacing service files.
3. **Upgrade all three fixed venvs.** Install and verify the same wheel independently for service, automation, and operator while every unit is stopped.
4. **Render and verify service-owned resources.** Render policies/templates, verify exact digests/ownership/modes, and place the matching service and client user units.
5. **Start and verify the service.** Start only the dedicated service user unit, then verify its canonical socket identity and policy.
6. **Start both updated client units.** Each client manager runs its matching fixed-venv helper to rebuild only its volatile endpoint; the helper does not wait for or manage the service.

## Three principals

The service, automation, and operator have disjoint UID and primary GID values. A supplemental `drawingmachine-connect` group grants only canonical runtime traversal and socket connection. It is not a primary GID and grants neither job-data nor managed-tree write authority.

Principal | Fixed venv and user unit | Owned runtime/data objects
--- | --- | ---
Service | owns its fixed venv; owns `drawingmachine.service` | configuration, state, database, canonical data/runtime roots, socket, serial session, and OpenClaw managed tree
Automation | owns its fixed venv; owns `drawingmachine-client-endpoint.service` | private runtime and persistent import/export endpoint links; temporary staging bundle, payload, and lease
Operator | owns its fixed venv; owns `drawingmachine-client-endpoint.service` | private volatile runtime endpoint link only; no staging or job-data objects

The automation staging bundle is automation-owned with the service group and mode `2770`; its payload and lease are `0640`. The service claims accepted material into service-private storage. Each client unit validates the exact service-access digest with its own fixed-venv helper and creates only the volatile symlink in its private runtime directory. Persistent automation import/export links are a separate, one-time cutover action and point to distinct service-owned roots.

D9 proves real `SO_PEERCRED` only for same-UID temporary processes. Its distinct service/automation/operator DAC and ACL identities are an explicit permission-oracle simulation: simulated DAC/ACL policy is not real cross-UID acceptance. Live enablement must independently prove real accounts, supplemental groups, traversal ACLs, endpoint ownership, and cross-UID connect/deny behavior.

## Client data flow

Only `--input-image` and the positional `gcode check` path use automatic staging. For either input, the CLI stages an ordinary caller-readable path automatically: it creates a bounded private prepare bundle, copies and hashes the stable regular input, writes a lease, atomically publishes a request bundle, and sends only the staging request ID/role. The service claims the published lease before use, records schema-v4 admission, moves accepted bytes into its private authority, and reconciles every terminal or crash path.

`--review-json` is read locally by the workflow CLI and is not staged. The CLI validates its object, reads the referenced processed image through the controlled export endpoint, verifies that exported artifact against job status, and sends the closed review object plus verified digest to the service.

Automation reads exported artifacts through the controlled export endpoint; operator and caller XDG data are never artifact authority. There is no shell-copy or watched dropbox workflow.

## XDG and controlled endpoints

The dedicated service account resolves `drawingmachine` beneath its XDG config, state, data, and runtime roots. The canonical socket is `drawingmachine/service.sock` beneath the service runtime root. Automation and operator receive distinct same-name endpoint symlinks beneath their own private XDG runtime roots. Client import/export endpoints likewise point to distinct canonical service-owned roots; source, endpoint, and target identities are revalidated during use.

## Packaged resource inventory

These are package facts, not instructions to write live paths:

- `systemd/drawingmachine.service` -> `%h/.config/systemd/user/drawingmachine.service` (0644)
- `systemd/drawingmachine-client-endpoint.service` -> `%h/.config/systemd/user/drawingmachine-client-endpoint.service` (0644)
- `install/service-access.toml.example` -> `/home/drawingmachine-service/.config/drawingmachine/service-access.toml` (0640)
- `install/openclaw-deployment.toml.example` -> `/home/drawingmachine-service/.config/drawingmachine/openclaw-deployment.toml` (0640)
- `install/config/application.toml.example` -> `/home/drawingmachine-service/.config/drawingmachine/config.toml` (0640)
- `install/config/machines/default.toml.example` -> `/home/drawingmachine-service/.config/drawingmachine/machines/default.toml` (0640)
- `install/config/providers/local-comfyui.toml.example` -> `/home/drawingmachine-service/.config/drawingmachine/providers/local-comfyui.toml` (0640)
- `install/config/providers/local-comfyui-workflow.json.example` -> `/home/drawingmachine-service/.config/drawingmachine/providers/local-comfyui-workflow.json` (0640)

## Separate live approvals

Separate approvals are required for identities/groups; home and XDG directories; ACLs/modes/owners; linger; unit placement/enable/start; real socket probes; OpenClaw registry/managed roots; provider network access; a serial-device value; and every physical HOME, ZCAL, ZCONFIRM, STREAM, HOME_Z, or recovery action. See [machine safety](machine-safety.md).
