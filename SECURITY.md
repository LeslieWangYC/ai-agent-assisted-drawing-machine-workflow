# Security policy

## Reporting

Report suspected vulnerabilities through GitHub private vulnerability
reporting (Security → Report a vulnerability) on this repository. Please do
not open public issues for security reports.

## Scope and model

The service is designed around strict local privilege separation:

- Three designed POSIX principals (service, automation, operator) with
  disjoint identities; cross-principal access flows only through validated
  Unix-socket endpoints and ACL-scoped filesystem exchange roots.
- Closed, strictly validated schemas for every protocol message, config file,
  and persisted record; digest pinning for installed resources and agent
  guidance documents.
- Machine motion requires per-action operator approval with bounded TTL;
  approval, phase, and milestone transitions are typed and fail-closed.
- No network listener: the protocol surface is local AF_UNIX only; the only
  outbound connection in a real deployment is the operator-configured local
  image-provider endpoint.

Security-relevant modules carry a 100% branch-coverage requirement enforced in
CI (`tests/architecture/package_c_safety_modules.txt`).

This public repository ships templates and placeholders only; it contains no
real deployment values. Findings about the internal deployment of the project
cannot be verified from this repository and are out of scope here.
