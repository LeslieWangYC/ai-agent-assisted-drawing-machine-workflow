import argparse
from collections.abc import Callable
from typing import Never

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import ProtocolResponse

from .config import check_config
from .gcode import check_gcode
from .jobs import cancel_job, job_status, wait_for_job
from .machine import machine_action, machine_prepare, machine_recover, machine_status
from .openclaw import openclaw_command
from .service import run_service, service_status
from .workflow import run_workflow

CommandHandler = Callable[[argparse.Namespace], ProtocolResponse]


class CliUsageError(DrawingMachineError):
    pass


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliUsageError(
            ErrorPayload(
                code="CLI_USAGE_ERROR",
                category=ErrorCategory.INPUT,
                message=message,
                retryable=False,
                details={},
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="drawingmachine")
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="result output format",
    )
    command_groups = parser.add_subparsers(dest="command_group", required=True)

    config_parser = command_groups.add_parser("config")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_check = config_commands.add_parser("check")
    config_check.set_defaults(handler=check_config, command_name="config.check")

    service_parser = command_groups.add_parser("service")
    service_commands = service_parser.add_subparsers(dest="service_command", required=True)
    service_run = service_commands.add_parser("run")
    service_run.set_defaults(handler=run_service, command_name="service.run")
    service_status_parser = service_commands.add_parser("status")
    service_status_parser.set_defaults(handler=service_status, command_name="service.status")

    workflow_parser = command_groups.add_parser("workflow")
    workflow_commands = workflow_parser.add_subparsers(dest="workflow_command", required=True)
    workflow_run = workflow_commands.add_parser("run")
    workflow_run.add_argument("--job-name")
    workflow_run.add_argument("--input-image")
    workflow_run.add_argument("--route-mode", choices=("auto", "direct", "image-edit"))
    workflow_run.add_argument("--semantic-assessment-json")
    prompt = workflow_run.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    workflow_run.add_argument("--resume-job")
    workflow_run.add_argument("--review-json")
    workflow_run.set_defaults(handler=run_workflow, command_name="workflow.run")

    job_parser = command_groups.add_parser("job")
    job_commands = job_parser.add_subparsers(dest="job_command", required=True)
    job_status_parser = job_commands.add_parser("status")
    job_status_parser.add_argument("job_id")
    job_status_parser.set_defaults(handler=job_status, command_name="job.status")
    job_wait = job_commands.add_parser("wait")
    job_wait.add_argument("job_id")
    job_wait.add_argument("--timeout-seconds", type=float, default=300.0)
    job_wait.add_argument("--poll-interval-seconds", type=float, default=0.25)
    job_wait.set_defaults(handler=wait_for_job, command_name="job.wait")
    job_cancel = job_commands.add_parser("cancel")
    job_cancel.add_argument("job_id")
    job_cancel.set_defaults(handler=cancel_job, command_name="job.cancel")

    gcode_parser = command_groups.add_parser("gcode")
    gcode_commands = gcode_parser.add_subparsers(dest="gcode_command", required=True)
    gcode_check = gcode_commands.add_parser("check")
    gcode_check.add_argument("path")
    gcode_check.add_argument("--timeout-seconds", type=float, default=300.0)
    gcode_check.add_argument("--poll-interval-seconds", type=float, default=0.25)
    gcode_check.set_defaults(handler=check_gcode, command_name="gcode.check")

    machine_parser = command_groups.add_parser("machine")
    machine_commands = machine_parser.add_subparsers(dest="machine_command", required=True)
    machine_status_parser = machine_commands.add_parser("status")
    machine_status_parser.add_argument("--execution-id")
    machine_status_parser.set_defaults(handler=machine_status, command_name="machine.status")
    machine_prepare_parser = machine_commands.add_parser("prepare")
    machine_prepare_parser.add_argument("job_id")
    machine_prepare_parser.add_argument("--job-revision", type=int, required=True)
    machine_prepare_parser.set_defaults(handler=machine_prepare, command_name="machine.prepare")
    for command in ("home", "zcal", "zconfirm", "stream", "home-z"):
        action_parser = machine_commands.add_parser(command)
        action_parser.add_argument("execution_id")
        action_parser.add_argument("--approve")
        action_parser.set_defaults(handler=machine_action, command_name=f"machine.{command}")
    machine_recover_parser = machine_commands.add_parser("recover")
    machine_recover_parser.add_argument("execution_id")
    machine_recover_parser.add_argument(
        "--disposition",
        choices=("release", "restart-sequence", "safe-home"),
        required=True,
    )
    machine_recover_parser.add_argument("--approve")
    machine_recover_parser.set_defaults(handler=machine_recover, command_name="machine.recover")

    openclaw_parser = command_groups.add_parser("openclaw")
    openclaw_commands = openclaw_parser.add_subparsers(dest="openclaw_command", required=True)
    for command in ("check", "install"):
        command_parser = openclaw_commands.add_parser(command)
        command_parser.add_argument("--openclaw-root", action="append", required=True)
        command_parser.set_defaults(handler=openclaw_command, command_name=f"openclaw.{command}")

    return parser
