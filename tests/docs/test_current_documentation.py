from __future__ import annotations

import hashlib
import json
import re
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from drawingmachine.cli.parser import build_parser

CURRENT_DOCUMENTS = {
    "architecture.md": ("## Status and authority", "## Module boundaries", "## Ownership boundaries"),
    "installation.md": ("## Status and authority", "## Fixed wheel layout", "## Three principals"),
    "configuration.md": ("## Status and authority", "## Strict TOML", "## Digest authority"),
    "cli-reference.md": ("## Status and authority", "## Global output", "## Installed command surface"),
    "openclaw-operation.md": ("## Status and authority", "## Command-only operation", "## Two roots"),
    "machine-safety.md": ("## Status and authority", "## Approval boundary", "## Motion milestones"),
    "recovery.md": ("## Status and authority", "## Staging recovery", "## Machine recovery"),
    "development.md": ("## Status and authority", "## Offline test architecture", "## No-live rule"),
}

STATUS = (
    "Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, "
    "OpenClaw registry, serial, and hardware actions require separate authorization."
)

EXPECTED_CLI = {
    "config check": (),
    "service run": (),
    "service status": (),
    "workflow run": (
        "--input-image",
        "--job-name",
        "--prompt",
        "--prompt-file",
        "--resume-job",
        "--review-json",
        "--route-mode",
        "--semantic-assessment-json",
    ),
    "job status": (),
    "job wait": ("--poll-interval-seconds", "--timeout-seconds"),
    "job cancel": (),
    "gcode check": ("--poll-interval-seconds", "--timeout-seconds"),
    "machine status": ("--execution-id",),
    "machine prepare": ("--job-revision",),
    "machine home": ("--approve",),
    "machine zcal": ("--approve",),
    "machine zconfirm": ("--approve",),
    "machine stream": ("--approve",),
    "machine home-z": ("--approve",),
    "machine recover": ("--approve", "--disposition"),
    "openclaw check": ("--openclaw-root",),
    "openclaw install": ("--openclaw-root",),
}

_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|nothing|cannot|forbid(?:s|den)?|without|does not|do not|must not|is not|are not|non-aliased|zero-write)\b"
)
_UNSAFE_AFFIRMATIONS = {
    "old executable or script": re.compile(
        r"(?:drawingmachine[_-]hardware|scripts/(?:openclaw_operator_bridge|stream_gcode_fluidnc)\.py)"
    ),
    "application import bypass": re.compile(r"(?:openclaw|operator|automation).*\bimports?\s+drawingmachine\b"),
    "root default": re.compile(r"(?:runs?\s+as\s+root|root\s+(?:user|default))"),
    "same principal approval": re.compile(r"same[- ]principal.*approval|approval.*same[- ]principal"),
    "raw hardware": re.compile(r"raw\s+(?:hardware|serial|fluidnc)|direct\s+serial|machine\s+raw"),
    "automatic operation retry": re.compile(r"automat(?:ic|ically).*(?:retry|resume)|(?:retry|resume).*automat"),
    "system service": re.compile(r"system\s+(?:daemon|service)"),
    "live completion claim": re.compile(
        r"live\s+(?:cutover|deployment|hardware).*(?:accepted|authorized|complete|deployed)"
    ),
    "caller XDG authority": re.compile(
        r"caller[- ]xdg.*(?:(?:writer|authority|read).*(?:openclaw|g[- ]?code|artifact)|g[- ]?code.*read)"
    ),
    "operator job access": re.compile(r"operator.*(?:read|access).*(?:job|artifact|data)"),
    "same UID acceptance": re.compile(r"same[- ]uid.*(?:accept|proves?).*cross[- ]principal"),
    "registry mutation": re.compile(r"registry.*(?:write|modify|replace)"),
    "manual staging prerequisite": re.compile(r"(?:shell|copy|dropbox).*(?:required|prerequisite)"),
    "orphan staging success": re.compile(r"orphan.*stag.*(?:ignore|delete|complete|success)"),
    "client-owned managed target": re.compile(
        r"(?:operator|automation)[- ]owned.*(?:managed|target|ancestor)|(?:operator|automation).*(?:owns?|writes?|replaces?).*(?:managed|target)"
    ),
    "local deployment writer": re.compile(r"local.*(?:deployment\s+handler|installer)"),
    "root alias": re.compile(r"registry.*(?:alias|same).*(?:managed|root)"),
    "writable managed root": re.compile(r"(?:operator|automation|client).*(?:writable|write).*(?:managed|root)"),
    "unbounded managed write": re.compile(r"managed\s+(?:directories|root).*(?<!service-)writable"),
    "permission window": re.compile(r"permission[- ]window|temporary\s+permission"),
    "privilege bypass": re.compile(r"\b(?:chown|chgrp|sudo|proxy)\b.*(?:run|use|command|bypass)"),
    "review JSON staging": re.compile(r"(?:--review-json.*stag|stag.*--review-json)"),
}
_UNSAFE_VALUES = {
    "invented machine command": re.compile(
        r"drawingmachine\s+machine\s+(?:raw|jog|unlock|offset|serial|bridge)\b|--serial-path\b"
    ),
    "numeric principal": re.compile(r"(?m)^\s*(?:uid|gid)\s*=\s*\d+"),
    "numeric runtime UID": re.compile(r"/run/user/\d+"),
    "serial device": re.compile(r"/(?:dev)/(?:tty|serial)[^\s`]*|\bCOM\d+\b"),
    "approval value": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"),
    "secret value": re.compile(r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*[^\s<]+"),
    "invented milestone": re.compile(r"\bstream_started\b"),
}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _assert_document_names(names: set[str]) -> None:
    assert names == set(CURRENT_DOCUMENTS), "the current operating set must contain exactly eight documents"


def _documents() -> dict[str, str]:
    root = _root() / "docs/current"
    assert root.is_dir(), "docs/current must contain the eight current operating documents"
    actual = {path.name for path in root.iterdir() if path.is_file()}
    _assert_document_names(actual)
    return {name: (root / name).read_text(encoding="utf-8") for name in CURRENT_DOCUMENTS}


def _section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


def _markdown_table(text: str, heading: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    lines = [line.strip() for line in _section(text, heading).splitlines()]
    table_start = next(index for index, line in enumerate(lines) if "|" in line)
    table: list[str] = []
    for line in lines[table_start:]:
        if not line:
            break
        table.append(line)
    assert len(table) >= 3 and set(table[1]) <= {"-", "|", " ", ":"}
    header = tuple(cell.strip() for cell in table[0].strip("|").split("|"))
    rows: dict[str, tuple[str, ...]] = {}
    for line in table[2:]:
        if "|" not in line:
            break
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        assert len(cells) == len(header)
        key = cells[0].strip("`")
        assert key not in rows
        rows[key] = cells[1:]
    return header, rows


def _cli_rows(reference: str) -> dict[str, tuple[str, ...]]:
    header, rows = _markdown_table(reference, "## Installed command surface")
    assert header == ("Command", "Optional flags shown by installed help")
    parsed: dict[str, tuple[str, ...]] = {}
    for command, (flags_cell,) in rows.items():
        assert command.startswith("drawingmachine ")
        flags = flags_cell.strip("`")
        parsed[command.removeprefix("drawingmachine ")] = () if flags == "none" else tuple(flags.split())
    return parsed


def _assert_safe_text(text: str) -> None:
    normalized = re.sub(r"[ \t]+", " ", text.casefold())
    for label, pattern in _UNSAFE_VALUES.items():
        assert not pattern.search(normalized), label
    for line in normalized.splitlines():
        for clause in re.split(r"(?<=[.!?])\s+|;", line):
            for label, pattern in _UNSAFE_AFFIRMATIONS.items():
                if pattern.search(clause):
                    assert _NEGATION.search(clause), label


def _assert_cli_mentions(text: str) -> None:
    mentions = re.findall(r"`(drawingmachine(?:\s+[^`]*)?)`", text)
    mentions.extend(re.findall(r"(?m)^(drawingmachine\s+.+)$", text))
    known_flags = {"--output", *(flag for flags in EXPECTED_CLI.values() for flag in flags)}
    for mention in mentions:
        assert set(re.findall(r"--[a-z][a-z-]+", mention)) <= known_flags
        tokens = mention.split()
        if len(tokens) == 1:
            continue
        if tokens[1:3] == ["--output", "json"]:
            tokens = [tokens[0], *tokens[3:]]
        if tokens[1:] == ["..."]:
            continue
        assert len(tokens) >= 3 and " ".join(tokens[1:3]) in EXPECTED_CLI


def _command_surface() -> dict[str, tuple[str, ...]]:
    surface: dict[str, tuple[str, ...]] = {}

    def visit(parser: Any, prefix: tuple[str, ...]) -> None:
        actions = parser._actions
        options = sorted(
            option for action in actions for option in action.option_strings if option not in {"-h", "--help"}
        )
        if prefix:
            surface[" ".join(prefix)] = tuple(options)
        for action in actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for name, child in choices.items():
                    visit(child, (*prefix, name))

    visit(build_parser(), ("drawingmachine",))
    return surface


def _installed_help_options(executable: Path, command: str = "") -> tuple[str, ...]:
    result = subprocess.run(
        [str(executable), *command.split(), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.startswith(f"usage: drawingmachine{' ' + command if command else ''}")
    return tuple(sorted(set(re.findall(r"--[a-z][a-z-]+", result.stdout)) - {"--help"}))


def test_exact_current_document_set_and_sections() -> None:
    documents = _documents()
    for name, headings in CURRENT_DOCUMENTS.items():
        text = documents[name]
        assert text.startswith("# ")
        assert STATUS in text
        assert all(heading in text for heading in headings)
        assert text.count("```") % 2 == 0


def test_installation_principal_ownership_and_rebuild_order_are_closed() -> None:
    installation = _documents()["installation.md"]
    header, rows = _markdown_table(installation, "## Three principals")
    assert header == ("Principal", "Fixed venv and user unit", "Owned runtime/data objects")
    assert set(rows) == {"Service", "Automation", "Operator"}
    assert "owns its fixed venv" in rows["Service"][0]
    assert "owns `drawingmachine.service`" in rows["Service"][0]
    assert "owns its fixed venv" in rows["Automation"][0]
    assert "owns `drawingmachine-client-endpoint.service`" in rows["Automation"][0]
    assert all(item in rows["Automation"][1] for item in ("bundle", "payload", "lease", "endpoint"))
    assert "owns its fixed venv" in rows["Operator"][0]
    assert "owns `drawingmachine-client-endpoint.service`" in rows["Operator"][0]

    steps = re.findall(r"(?m)^\d+\. \*\*([^*]+)\*\*", _section(installation, "## Rebuild order"))
    assert steps == [
        "Stop both client units.",
        "Stop the service unit.",
        "Upgrade all three fixed venvs.",
        "Render and verify service-owned resources.",
        "Start and verify the service.",
        "Start both updated client units.",
    ]


def test_documented_runtime_facts_match_live_boundaries() -> None:
    documents = _documents()
    installation = documents["installation.md"]
    assert "Only `--input-image` and the positional `gcode check` path use automatic staging." in installation
    assert "`--review-json` is read locally by the workflow CLI and is not staged." in installation

    architecture = documents["architecture.md"]
    assert "no mutable client, runtime, or external filesystem I/O" in architecture
    assert "installed OpenClaw package-resource streams" in architecture

    machine = documents["machine-safety.md"]
    assert "`NOT_STARTED` to `FIRST_WRITE_POSSIBLE` to `STREAM_CONFIRMED`" in machine

    configuration = documents["configuration.md"]
    assert "validates only the application, machine, and provider documents" in configuration
    assert "does not validate `service-access.toml` or `openclaw-deployment.toml`" in configuration

    cli = documents["cli-reference.md"]
    workflow = _section(cli, "## Workflow contracts")
    assert all(
        claim in workflow
        for claim in (
            "Initial submission requires `--job-name`, `--input-image`, and `--route-mode`.",
            "`auto` additionally requires `--semantic-assessment-json`.",
            "Resume requires both `--resume-job` and `--review-json` and rejects every initial-submission argument.",
            "`--prompt` and `--prompt-file` are mutually exclusive.",
        )
    )


def test_cli_reference_matches_actual_parser_surface() -> None:
    reference = _documents()["cli-reference.md"]
    rows = _cli_rows(reference)
    surface = _command_surface()
    assert surface["drawingmachine"] == ("--output",)
    assert (
        {key.removeprefix("drawingmachine "): value for key, value in surface.items() if key.count(" ") == 2}
        == EXPECTED_CLI
        == rows
    )
    assert "`--output {text,json}`" in reference
    assert "one closed JSON object on stdout" in reference


def test_cli_reference_matches_fresh_installed_help(installed_drawingmachine: Path) -> None:
    rows = _cli_rows(_documents()["cli-reference.md"])
    assert _installed_help_options(installed_drawingmachine) == ("--output",)
    assert {command: _installed_help_options(installed_drawingmachine, command) for command in rows} == rows


def test_packaged_resource_and_configuration_facts_are_current() -> None:
    documents = _documents()
    installation = documents["installation.md"]
    configuration = documents["configuration.md"]
    openclaw = documents["openclaw-operation.md"]
    package = resources.files("drawingmachine.resources")
    install_manifest = json.loads(package.joinpath("install/manifest.json").read_text(encoding="utf-8"))
    openclaw_manifest_bytes = package.joinpath("openclaw/manifest.json").read_bytes()
    registry_policy_bytes = package.joinpath("openclaw/registry-policy.json").read_bytes()
    service_unit = package.joinpath("systemd/drawingmachine.service").read_text(encoding="utf-8")
    client_unit = package.joinpath("systemd/drawingmachine-client-endpoint.service").read_text(encoding="utf-8")

    assert len(install_manifest["resources"]) == 8
    assert "`%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run`" in installation
    assert "ExecStart=%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run" in service_unit
    assert "drawingmachine.install.client_endpoint runtime" in client_unit
    for entry in install_manifest["resources"]:
        assert f"`{entry['resource']}` -> `{entry['destination']}` ({entry['mode']})" in installation

    assert f"`{hashlib.sha256(openclaw_manifest_bytes).hexdigest()}`" in configuration
    assert f"`{hashlib.sha256(registry_policy_bytes).hexdigest()}`" in configuration
    assert "application, machine, and provider byte digests enter job configuration authority" in configuration
    assert (
        "service-access and OpenClaw deployment-policy digests do not enter job or machine authority" in configuration
    )
    assert "CHECK_ONLY_REGISTRY_POLICY" in openclaw
    assert "four managed targets and one check-only registry policy" in openclaw


def test_security_and_recovery_claims_are_explicit() -> None:
    documents = _documents()
    combined = "\n".join(documents.values())
    required = {
        "installation.md": (
            "service, automation, and operator have disjoint UID and primary GID values",
            "D9 proves real `SO_PEERCRED` only for same-UID temporary processes",
            "simulated DAC/ACL policy is not real cross-UID acceptance",
            "the CLI stages an ordinary caller-readable path automatically",
            "the service claims the published lease before use",
        ),
        "openclaw-operation.md": (
            "The registry root is read-only; the managed root is distinct and service-writable.",
            "managed directories are service-owned setgid `2750`",
            "managed targets are service-owned `0640`",
            "No operator, local user, or automation process writes the managed tree.",
            "No shell, copy, or dropbox step is a prerequisite.",
            "Never use chown, chgrp, sudo, or a proxy to bypass this boundary.",
        ),
        "machine-safety.md": (
            "HOME, ZCAL, ZCONFIRM, STREAM, and HOME_Z each require a distinct one-time approval",
            "automation cannot consume operator approval",
            "code completion is not live hardware acceptance",
            "No raw hardware command surface exists",
        ),
        "recovery.md": (
            "No automatic retry or resume is authorized.",
            "STREAM_CONFIRMED is irreversible",
            "an orphaned partial-frame staging lease is reconciled or blocks its role",
        ),
    }
    for name, claims in required.items():
        text = documents[name].casefold()
        assert all(claim.casefold() in text for claim in claims)
    _assert_safe_text(combined)
    _assert_cli_mentions(combined)
    assert not re.search(r"/home/(?!drawingmachine-service\b)[A-Za-z0-9_-]+", combined)
    assert not re.search(r"/run/user/\d+", combined)


@pytest.mark.parametrize(
    "mutation",
    (
        "Run `drawingmachine machine raw --serial-path /tmp/controller`.",
        "Use scripts/openclaw_operator_bridge.py for execution.",
        "Automation imports drawingmachine directly.",
        "The service runs as root by default.",
        "The same principal requests and consumes approval.",
        "Raw serial commands are available to callers.",
        "The system service automatically resumes jobs after failure.",
        "Install a system daemon for Drawingmachine.",
        "Live cutover is complete and deployed.",
        "Caller-XDG is writer authority for OpenClaw.",
        "Operator may read job artifacts.",
        "Caller-XDG may read G-code artifacts.",
        "Same-UID proves accepted cross-principal access.",
        "The registry writer modifies openclaw.json.",
        "A shell copy into the dropbox is required.",
        "An orphan staging lease is ignored and called complete.",
        "The operator-owned managed target is accepted.",
        "Automation owns and replaces the managed target.",
        "Use a local deployment handler for OpenClaw.",
        "The registry root aliases the managed root.",
        "Automation has writable access to the managed root.",
        "Managed directories are writable by clients.",
        "Open a temporary permission window during install.",
        "Use sudo command and chown to bypass ownership.",
        "The CLI stages `--review-json` before resume.",
        "The stream milestone becomes STREAM_STARTED.",
        "uid = 1000",
        "gid = 1000",
        "runtime = /run/user/1000/drawingmachine",
        "serial = /dev/ttyUSB0",
        "approval = 12345678-1234-4234-8234-123456789abc",
        "token = actual-value",
        "secret: actual-value",
    ),
)
def test_dangerous_documentation_mutations_are_rejected(mutation: str) -> None:
    with pytest.raises(AssertionError):
        _assert_safe_text(mutation)


def test_cli_table_mutations_and_ninth_document_are_rejected() -> None:
    reference = _documents()["cli-reference.md"]
    with pytest.raises(AssertionError):
        assert _cli_rows(reference.replace("`none`", "`--invented`", 1)) == EXPECTED_CLI
    with pytest.raises(AssertionError):
        assert (
            _cli_rows(
                reference.replace(
                    "`drawingmachine config check` | `none`",
                    "`drawingmachine config check` | `none`\n`drawingmachine machine raw` | `--serial-path`",
                )
            )
            == EXPECTED_CLI
        )
    with pytest.raises(AssertionError):
        _assert_document_names({*CURRENT_DOCUMENTS, "ninth.md"})


def test_markdown_links_are_local_and_resolve() -> None:
    root = _root()
    for text in _documents().values():
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            assert not target.startswith(("http://", "https://", "/"))
            path = (root / "docs/current" / target.split("#", 1)[0]).resolve()
            assert path.is_relative_to(root) and path.exists()
