from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from drawingmachine.cli.gcode import check_gcode
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def test_gcode_cli_stages_existing_path_and_reads_static_result_from_export(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "caller-private.gcode"
    source.write_bytes(b"G21\n")
    staged_path = tmp_path / "imports/gcode/drop/bundle.stage/payload"
    staged = SimpleNamespace(payload_path=staged_path)
    contents = json.dumps({"ok": True}).encode()
    decoy = tmp_path / "caller-xdg/jobs/job/artifacts/attempt/gcode_static.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text('{"ok": false}', encoding="utf-8")
    artifact = {
        "role": "gcode_static",
        "relative_path": "artifacts/attempt/gcode_static.json",
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size_bytes": len(contents),
        "media_type": "application/json",
    }
    events: list[tuple[str, object]] = []
    responses = iter(
        (
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "gcode.check",
                "submit",
                {"schema_version": 1, "job_id": "job", "state": "QUEUED", "static_result": None},
                None,
            ),
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "job.status",
                "status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "job", "state": "COMPLETED"},
                    "artifacts": [artifact],
                    "cancellation_requested": False,
                },
                None,
            ),
        )
    )

    class Service:
        def call(self, command: str, arguments: object, **kwargs: object) -> ProtocolResponse:
            events.append((command, arguments))
            callback = kwargs.get("before_write")
            if callable(callback):
                callback()
            return next(responses)

    class Staging:
        def stage_gcode(self, path: Path, request_id: str, clock: object):
            events.append(("stage", (path, request_id, clock)))
            return staged

        def transfer_before_write(self, value: object):
            events.append(("transfer", value))
            return value

        def discard_definitely_unsent(self, value: object, outcome: object) -> None:
            raise AssertionError((value, outcome))

        def read_exported_artifact(self, job_id: str, reference: object) -> bytes:
            events.append(("export", (job_id, reference)))
            return contents

    args = argparse.Namespace(
        path=str(source),
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
    )
    monkeypatch.setattr("drawingmachine.cli.service.staging_clock", object)

    response = check_gcode(
        args,
        client=Service(),
        client_staging=Staging(),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )

    assert response.ok is True
    assert response.data["static_result"] == {"ok": True}
    assert json.loads(decoy.read_text(encoding="utf-8")) == {"ok": False}
    assert events[1] == ("gcode.check", {"schema_version": 1, "path": str(staged_path)})
    assert [event[0] for event in events] == ["stage", "gcode.check", "transfer", "job.status", "export"]
