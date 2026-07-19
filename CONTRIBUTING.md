# Contributing

This project is developed fully offline. All contributions must keep that
property: no test or tool may contact a network endpoint, real serial device,
system service manager, or agent-platform installation, and none may create or
modify real accounts, groups, ACLs, or user data. The guards that enforce this
are part of the test suite.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]' wheel setuptools
```

## Gates

Run focused tests first, then the complete battery. A change is complete only
when all of the following pass (the CI workflow runs the same set):

```bash
python -m pytest -q
python -m build
python -m pytest --cov=drawingmachine --cov-branch --cov-report=json:coverage.json --cov-fail-under=90
python tools/check_branch_coverage.py coverage.json
python tools/check_safety_module_coverage.py coverage.json tests/architecture/package_c_safety_modules.txt --minimum 100
python -m mypy --strict src/drawingmachine
python -m ruff check --select E4,E7,E9,F,I,UP,B,SIM,RUF src/drawingmachine tests tools
python -m ruff format --check src/drawingmachine tests tools
lint-imports
```

Safety-relevant modules require 100% branch coverage; do not weaken strict
schemas, delete fixtures, or skip tests to make a gate pass. See
[docs/current/development.md](docs/current/development.md) for the complete
rules, including fixture determinism and the no-live rule.

## Scope

Generic workflow, safety, and adapter improvements are welcome. Changes that
would embed personal or site-specific values (home paths, account names,
numeric identities, device serials, endpoints, machine calibration) are
rejected by documentation and fixture guards — keep all examples templated.
