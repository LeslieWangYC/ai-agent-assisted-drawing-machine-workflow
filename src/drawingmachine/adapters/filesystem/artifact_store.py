from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.adapters.filesystem._artifact_validation import (
    ValidatedManifest,
    artifact_error,
    copy_declared_tree,
    stage_external_file,
    validate_component,
    validate_manifests,
    validate_prepared_tree,
    validate_relative_path,
    validate_text,
    verify_source_chain,
)
from drawingmachine.config import XdgPaths
from drawingmachine.domain.jobs import ArtifactRef, JobRecord
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonValue
from drawingmachine.ports.artifacts import ExpectedArtifact, PromotedBundle, WorkerArtifact


def _track(stack: ExitStack, descriptor: int) -> int:
    stack.callback(os.close, descriptor)
    return descriptor


def _translated(error: BaseException, *, job_id: str | None = None) -> DrawingMachineError:
    if isinstance(error, DrawingMachineError):
        return error
    code = "ARTIFACT_PATH_INVALID" if isinstance(error, _secure_fs.SecureFilesystemError) else "ARTIFACT_IO_FAILED"
    return artifact_error(str(error), code=code, job_id=job_id)


class FilesystemArtifactStore:
    """Secure filesystem store for a quiescent producer and serialized service writer.

    Before promotion, the producer process has exited and staging is no longer being
    changed. The drawingmachine service is the sole artifact and projection writer.
    Arbitrary malicious same-UID processes are outside the threat model because they can
    directly mutate final artifacts and SQLite after any finite validation. This adapter
    protects against untrusted staging contents, link and path replacement, collisions,
    crashes, and accidental stale or raced service-owned paths. The hidden prepared copy
    is service-owned; post-publication validation is defense-in-depth.
    """

    def __init__(self, paths: XdgPaths) -> None:
        self._paths = paths
        try:
            _secure_fs.require_secure_platform()
        except _secure_fs.SecurePlatformUnavailable as error:
            raise artifact_error(
                f"secure filesystem primitives are unavailable: {error}",
                code="ARTIFACT_PLATFORM_UNSUPPORTED",
            ) from error

    @property
    def jobs_dir(self) -> Path:
        return self._paths.jobs_dir

    def create_staging(self, job_id: str, attempt_id: str) -> Path:
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                staging_root_fd = _track(stack, _secure_fs.ensure_directory_at(root.jobs_fd, ".staging"))
                staging_job_fd = _track(stack, _secure_fs.ensure_directory_at(staging_root_fd, job_id))
                try:
                    staging_fd = _track(stack, _secure_fs.create_directory_at(staging_job_fd, attempt_id))
                except FileExistsError as error:
                    raise artifact_error(
                        "artifact staging attempt already exists",
                        code="ARTIFACT_STAGING_COLLISION",
                        job_id=job_id,
                        details={"attempt_id": attempt_id},
                    ) from error
                _secure_fs.fsync_directory(staging_fd)
                _secure_fs.fsync_directory(staging_job_fd)
                verify_source_chain(
                    root,
                    staging_root_fd,
                    staging_job_fd,
                    staging_fd,
                    job_id,
                    attempt_id,
                )
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error
        return self.jobs_dir / ".staging" / job_id / attempt_id

    def discard_staging(self, job_id: str, attempt_id: str) -> None:
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                try:
                    staging_root_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, ".staging"))
                    staging_job_fd = _track(stack, _secure_fs.open_directory_at(staging_root_fd, job_id))
                except FileNotFoundError:
                    return
                _secure_fs.remove_tree_at(staging_job_fd, attempt_id)
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error

    def discard_promoted_bundle(
        self,
        job_id: str,
        attempt_id: str,
        exact_artifacts: tuple[ArtifactRef, ...],
    ) -> None:
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        if (
            type(exact_artifacts) is not tuple
            or not exact_artifacts
            or any(type(artifact) is not ArtifactRef for artifact in exact_artifacts)
        ):
            raise artifact_error("promoted revocation authority must contain exact artifact references", job_id=job_id)
        workers: list[WorkerArtifact] = []
        expected: list[ExpectedArtifact] = []
        for artifact in exact_artifacts:
            relative = validate_relative_path(
                artifact.relative_path,
                field="promoted revocation artifact path",
                job_id=job_id,
            )
            if len(relative.parts) < 3 or relative.parts[:2] != ("artifacts", attempt_id):
                raise artifact_error(
                    "promoted revocation artifact is outside the exact attempt",
                    code="ARTIFACT_PATH_INVALID",
                    job_id=job_id,
                )
            attempt_relative = PurePosixPath(*relative.parts[2:]).as_posix()
            workers.append(
                WorkerArtifact(
                    artifact.role,
                    attempt_relative,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.media_type,
                )
            )
            expected.append(ExpectedArtifact(artifact.role, attempt_relative, artifact.media_type, False))
        manifest = validate_manifests(tuple(workers), tuple(expected), job_id=job_id)
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                try:
                    job_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, job_id))
                    artifacts_fd = _track(stack, _secure_fs.open_directory_at(job_fd, "artifacts"))
                    try:
                        _secure_fs.stat_at(artifacts_fd, attempt_id)
                    except FileNotFoundError:
                        return
                    attempt_fd = _track(stack, _secure_fs.open_directory_at(artifacts_fd, attempt_id))
                except FileNotFoundError:
                    return
                current = validate_prepared_tree(
                    attempt_fd,
                    manifest,
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
                if current != tuple(sorted(exact_artifacts, key=lambda item: (item.role, item.relative_path))):
                    raise artifact_error("promoted revocation authority does not match the exact bundle", job_id=job_id)
                root.verify()
                _secure_fs.verify_entry_identity(root.jobs_fd, job_id, job_fd, directory=True)
                _secure_fs.verify_entry_identity(job_fd, "artifacts", artifacts_fd, directory=True)
                _secure_fs.verify_entry_identity(artifacts_fd, attempt_id, attempt_fd, directory=True)
                detached = _secure_fs.hidden_name("revoke")
                _secure_fs.rename_noreplace(artifacts_fd, attempt_id, artifacts_fd, detached)
                _secure_fs.fsync_directory(artifacts_fd)
                _secure_fs.verify_entry_identity(artifacts_fd, detached, attempt_fd, directory=True)
                _secure_fs.remove_tree_at(artifacts_fd, detached)
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error

    def import_file(
        self,
        job_id: str,
        attempt_id: str,
        source: Path,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> PromotedBundle:
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        role = validate_text(role, "artifact role", job_id=job_id)
        media_type = validate_text(media_type, "artifact media type", job_id=job_id)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise artifact_error("artifact import maximum must be a positive integer", job_id=job_id)
        relative = validate_relative_path(relative_path, field="artifact relative path", job_id=job_id)
        try:
            source_fd, source_before = _secure_fs.open_external_regular(source)
        except _secure_fs.SecureFilesystemError as error:
            raise _translated(error, job_id=job_id) from error
        try:
            if source_before.st_size > max_bytes:
                raise artifact_error("artifact source exceeds the maximum import size", job_id=job_id)
            copied = stage_external_file(
                self._paths.data_dir,
                job_id,
                attempt_id,
                source,
                source_fd,
                source_before,
                relative.parts,
                max_bytes=max_bytes,
            )
        except FileExistsError as error:
            raise artifact_error(
                "artifact staging attempt already exists",
                code="ARTIFACT_STAGING_COLLISION",
                job_id=job_id,
            ) from error
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error
        finally:
            os.close(source_fd)
        return self.validate_and_promote(
            job_id,
            attempt_id,
            (
                WorkerArtifact(
                    role=role,
                    relative_path=relative.as_posix(),
                    sha256=copied.sha256,
                    size_bytes=copied.size_bytes,
                    media_type=media_type,
                ),
            ),
            expected=(ExpectedArtifact(role, relative.as_posix(), media_type, False),),
        )

    def read_staged_bytes(
        self,
        job_id: str,
        attempt_id: str,
        artifact: WorkerArtifact,
        *,
        expected_relative_path: str,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        """Read a declared quiescent worker output without pathname authority."""
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        if not isinstance(artifact, WorkerArtifact):
            raise artifact_error("worker artifact descriptor is invalid", job_id=job_id)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise artifact_error("artifact read maximum must be a positive integer", job_id=job_id)
        expected_media_type = validate_text(
            expected_media_type,
            "expected artifact media type",
            job_id=job_id,
        )
        expected_relative = validate_relative_path(
            expected_relative_path,
            field="expected staging artifact relative path",
            job_id=job_id,
        )
        validate_manifests(
            (artifact,),
            (
                ExpectedArtifact(
                    artifact.role,
                    expected_relative.as_posix(),
                    expected_media_type,
                    False,
                ),
            ),
            job_id=job_id,
        )
        if artifact.size_bytes > max_bytes:
            raise artifact_error("staging artifact exceeds the maximum read size", job_id=job_id)
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                staging_root_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, ".staging"))
                staging_job_fd = _track(stack, _secure_fs.open_directory_at(staging_root_fd, job_id))
                current_fd = _track(stack, _secure_fs.open_directory_at(staging_job_fd, attempt_id))
                chain: list[tuple[int, str, int]] = [
                    (root.jobs_fd, ".staging", staging_root_fd),
                    (staging_root_fd, job_id, staging_job_fd),
                    (staging_job_fd, attempt_id, current_fd),
                ]
                for component in expected_relative.parts[:-1]:
                    parent_fd = current_fd
                    current_fd = _track(stack, _secure_fs.open_directory_at(parent_fd, component))
                    chain.append((parent_fd, component, current_fd))
                metadata, contents = _secure_fs.read_stable_regular_bounded(
                    current_fd,
                    expected_relative.parts[-1],
                    max_bytes=max_bytes,
                    nonblocking=True,
                )
                if metadata.size_bytes != artifact.size_bytes or metadata.sha256 != artifact.sha256:
                    raise artifact_error(
                        "staging artifact metadata does not match its declared size and digest",
                        job_id=job_id,
                    )
                root.verify()
                for parent_fd, name, descriptor in chain:
                    _secure_fs.verify_entry_identity(parent_fd, name, descriptor, directory=True)
                final_status = _secure_fs.stat_at(current_fd, expected_relative.parts[-1])
                if _secure_fs.stable_file_identity(final_status) != metadata.identity:
                    raise artifact_error("staging artifact identity changed after the validated read", job_id=job_id)
                return contents
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise artifact_error(
                f"staging artifact cannot be read safely: {error}",
                code="ARTIFACT_PATH_INVALID",
                job_id=job_id,
            ) from error

    def validate_and_promote(
        self,
        job_id: str,
        attempt_id: str,
        artifacts: tuple[WorkerArtifact, ...],
        *,
        expected: tuple[ExpectedArtifact, ...],
    ) -> PromotedBundle:
        """Promote only after the producer process has exited and staging is quiescent.

        The B5 supervisor must enforce this precondition before returning WorkerOutcome.
        """
        job_id = validate_component(job_id, "job_id")
        attempt_id = validate_component(attempt_id, "attempt_id")
        try:
            manifest = validate_manifests(artifacts, expected, job_id=job_id)
        except DrawingMachineError:
            self._cleanup_staging(job_id, attempt_id)
            raise
        try:
            promoted_artifacts = self._prepare_and_publish(job_id, attempt_id, manifest)
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error
        return PromotedBundle(
            job_id=job_id,
            attempt_id=attempt_id,
            root=self.jobs_dir / job_id / "artifacts" / attempt_id,
            artifacts=promoted_artifacts,
        )

    def read_bytes(
        self,
        job_id: str,
        artifact: ArtifactRef,
        *,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        """Read exact promoted bytes without resolving and reopening a pathname."""
        job_id = validate_component(job_id, "job_id")
        if not isinstance(artifact, ArtifactRef):
            raise artifact_error("persisted artifact reference is invalid", job_id=job_id)
        validated = ArtifactRef.from_json(artifact.to_json())
        expected_media_type = validate_text(expected_media_type, "expected artifact media type", job_id=job_id)
        if validated.media_type != expected_media_type:
            raise artifact_error("persisted artifact media type does not match the expected media type", job_id=job_id)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise artifact_error("artifact maximum read size must be a positive integer", job_id=job_id)
        relative = validate_relative_path(
            validated.relative_path,
            field="persisted artifact relative path",
            job_id=job_id,
        )
        if len(relative.parts) < 3 or relative.parts[0] != "artifacts":
            raise artifact_error(
                "persisted artifact must be job-root-relative under artifacts",
                code="ARTIFACT_PATH_INVALID",
                job_id=job_id,
            )
        attempt_id = validate_component(relative.parts[1], "artifact attempt_id")
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                job_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, job_id))
                artifacts_fd = _track(stack, _secure_fs.open_directory_at(job_fd, "artifacts"))
                current_fd = _track(stack, _secure_fs.open_directory_at(artifacts_fd, attempt_id))
                chain: list[tuple[int, str, int]] = [
                    (root.jobs_fd, job_id, job_fd),
                    (job_fd, "artifacts", artifacts_fd),
                    (artifacts_fd, attempt_id, current_fd),
                ]
                for component in relative.parts[2:-1]:
                    parent_fd = current_fd
                    current_fd = _track(stack, _secure_fs.open_directory_at(parent_fd, component))
                    chain.append((parent_fd, component, current_fd))
                metadata, contents = _secure_fs.read_stable_regular_bounded(
                    current_fd,
                    relative.parts[-1],
                    max_bytes=max_bytes,
                )
                if metadata.size_bytes != validated.size_bytes or metadata.sha256 != validated.sha256:
                    raise artifact_error(
                        "persisted artifact metadata does not match its durable size and digest",
                        job_id=job_id,
                    )
                root.verify()
                for parent_fd, name, descriptor in chain:
                    _secure_fs.verify_entry_identity(parent_fd, name, descriptor, directory=True)
                final_status = _secure_fs.stat_at(current_fd, relative.parts[-1])
                if _secure_fs.stable_file_identity(final_status) != metadata.identity:
                    raise artifact_error("persisted artifact identity changed after the validated read", job_id=job_id)
                return contents
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise artifact_error(
                f"persisted artifact cannot be read safely: {error}",
                code="ARTIFACT_PATH_INVALID",
                job_id=job_id,
            ) from error

    def resolve(self, job_id: str, artifact: ArtifactRef) -> Path:
        """Revalidate persisted metadata through descriptor-relative, no-follow traversal."""
        job_id = validate_component(job_id, "job_id")
        relative = validate_relative_path(
            artifact.relative_path,
            field="persisted artifact relative path",
            job_id=job_id,
        )
        if len(relative.parts) < 3 or relative.parts[0] != "artifacts":
            raise artifact_error(
                "persisted artifact must be job-root-relative under artifacts",
                code="ARTIFACT_PATH_INVALID",
                job_id=job_id,
            )
        attempt_id = validate_component(relative.parts[1], "artifact attempt_id")
        try:
            with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
                job_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, job_id))
                artifacts_fd = _track(stack, _secure_fs.open_directory_at(job_fd, "artifacts"))
                current_fd = _track(stack, _secure_fs.open_directory_at(artifacts_fd, attempt_id))
                chain: list[tuple[int, str, int]] = [
                    (root.jobs_fd, job_id, job_fd),
                    (job_fd, "artifacts", artifacts_fd),
                    (artifacts_fd, attempt_id, current_fd),
                ]
                for component in relative.parts[2:-1]:
                    parent_fd = current_fd
                    current_fd = _track(stack, _secure_fs.open_directory_at(parent_fd, component))
                    chain.append((parent_fd, component, current_fd))
                file_fd = _track(stack, _secure_fs.open_regular_at(current_fd, relative.parts[-1]))
                before = os.fstat(file_fd)
                metadata = _secure_fs.inspect_descriptor_metadata(file_fd)
                after = os.fstat(file_fd)
                if (
                    metadata.size_bytes != artifact.size_bytes
                    or metadata.sha256 != artifact.sha256
                    or _secure_fs.stable_file_identity(before) != _secure_fs.stable_file_identity(after)
                ):
                    raise artifact_error(
                        "persisted artifact changed or does not match its durable reference",
                        job_id=job_id,
                    )
                root.verify()
                for parent_fd, name, descriptor in chain:
                    _secure_fs.verify_entry_identity(parent_fd, name, descriptor, directory=True)
                _secure_fs.verify_entry_identity(current_fd, relative.parts[-1], file_fd, directory=False)
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise artifact_error(
                f"persisted artifact cannot be resolved safely: {error}",
                code="ARTIFACT_PATH_INVALID",
                job_id=job_id,
            ) from error
        return self.jobs_dir / job_id / Path(*relative.parts)

    def _prepare_and_publish(
        self,
        job_id: str,
        attempt_id: str,
        manifest: ValidatedManifest,
    ) -> tuple[ArtifactRef, ...]:
        with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
            staging_root_fd = _track(stack, _secure_fs.open_directory_at(root.jobs_fd, ".staging"))
            staging_job_fd = _track(stack, _secure_fs.open_directory_at(staging_root_fd, job_id))
            staging_fd = _track(stack, _secure_fs.open_directory_at(staging_job_fd, attempt_id))
            job_fd = _track(stack, _secure_fs.ensure_directory_at(root.jobs_fd, job_id))
            artifacts_fd = _track(stack, _secure_fs.ensure_directory_at(job_fd, "artifacts"))
            if not _secure_fs.same_filesystem(staging_job_fd, artifacts_fd):
                raise artifact_error(
                    "staging and promoted artifact parents must be on the same filesystem",
                    code="ARTIFACT_CROSS_DEVICE",
                    job_id=job_id,
                )
            try:
                _secure_fs.stat_at(artifacts_fd, attempt_id)
            except FileNotFoundError:
                pass
            else:
                raise artifact_error(
                    "promoted attempt already exists",
                    code="ARTIFACT_PROMOTION_COLLISION",
                    job_id=job_id,
                )
            prepared_name, prepared_fd = self._create_prepared(stack, artifacts_fd, attempt_id)
            try:
                copy_declared_tree(staging_fd, prepared_fd, manifest, job_id=job_id)
                prepublish_artifacts = validate_prepared_tree(
                    prepared_fd,
                    manifest,
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
                _secure_fs.fsync_directory(prepared_fd)
                _secure_fs.fsync_directory(artifacts_fd)
                self._verify_promotion_chains(
                    root,
                    staging_root_fd,
                    staging_job_fd,
                    staging_fd,
                    job_fd,
                    artifacts_fd,
                    prepared_fd,
                    prepared_name,
                    job_id,
                    attempt_id,
                )
            except BaseException:
                _secure_fs.remove_tree_at(artifacts_fd, prepared_name)
                _secure_fs.remove_tree_at(staging_job_fd, attempt_id)
                raise
            try:
                _secure_fs.rename_noreplace(artifacts_fd, prepared_name, artifacts_fd, attempt_id)
            except FileExistsError as error:
                _secure_fs.remove_tree_at(artifacts_fd, prepared_name)
                raise artifact_error(
                    "promoted attempt already exists",
                    code="ARTIFACT_PROMOTION_COLLISION",
                    job_id=job_id,
                ) from error
            except Exception as error:
                raise _translated(error, job_id=job_id) from error
            _secure_fs.fsync_directory(artifacts_fd)
            self._verify_promotion_chains(
                root,
                staging_root_fd,
                staging_job_fd,
                staging_fd,
                job_fd,
                artifacts_fd,
                prepared_fd,
                attempt_id,
                job_id,
                attempt_id,
            )
            try:
                final_artifacts = validate_prepared_tree(
                    prepared_fd,
                    manifest,
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
            except BaseException as error:
                raise artifact_error(
                    f"post-publication artifact validation failed: {error}",
                    job_id=job_id,
                ) from error
            if final_artifacts != prepublish_artifacts:
                raise artifact_error(
                    "post-publication artifact metadata does not match the prepared validation",
                    job_id=job_id,
                )
            _secure_fs.verify_entry_identity(
                artifacts_fd,
                attempt_id,
                prepared_fd,
                directory=True,
            )
            _secure_fs.remove_tree_at(staging_job_fd, attempt_id)
            return final_artifacts

    def _create_prepared(self, stack: ExitStack, artifacts_fd: int, attempt_id: str) -> tuple[str, int]:
        for _unused in range(8):
            name = _secure_fs.hidden_name(f"promoting-{attempt_id}")
            try:
                return name, _track(stack, _secure_fs.create_directory_at(artifacts_fd, name))
            except FileExistsError:
                continue
        raise artifact_error("cannot allocate a unique prepared artifact directory")

    def _cleanup_staging(self, job_id: str, attempt_id: str) -> None:
        try:
            self.discard_staging(job_id, attempt_id)
        except DrawingMachineError:
            return

    @classmethod
    def _verify_promotion_chains(
        cls,
        root: _secure_fs.ManagedJobsRoot,
        staging_root_fd: int,
        staging_job_fd: int,
        staging_fd: int,
        job_fd: int,
        artifacts_fd: int,
        prepared_fd: int,
        prepared_name: str,
        job_id: str,
        attempt_id: str,
    ) -> None:
        verify_source_chain(root, staging_root_fd, staging_job_fd, staging_fd, job_id, attempt_id)
        _secure_fs.verify_entry_identity(root.jobs_fd, job_id, job_fd, directory=True)
        _secure_fs.verify_entry_identity(job_fd, "artifacts", artifacts_fd, directory=True)
        _secure_fs.verify_entry_identity(artifacts_fd, prepared_name, prepared_fd, directory=True)

    def write_projection(self, job: JobRecord, artifacts: tuple[ArtifactRef, ...]) -> None:
        """Write under the serialized single-writer contract.

        The public identity and bytes are checked after replacement; concurrent same-UID
        mutation is outside this contract and requires a stronger operating-system boundary.
        """
        job_id = validate_component(job.job_id, "job_id")
        artifact_values: list[JsonValue] = []
        for artifact in sorted(
            artifacts,
            key=lambda item: (item.role, item.relative_path, item.sha256, item.size_bytes, item.media_type),
        ):
            relative = validate_relative_path(
                artifact.relative_path,
                field="projection artifact relative path",
                job_id=job_id,
            )
            if not relative.parts or relative.parts[0] != "artifacts":
                raise artifact_error("projection artifact must be job-root-relative under artifacts", job_id=job_id)
            artifact_values.append(artifact.to_json())
        payload = job.to_json()
        payload["artifacts"] = artifact_values
        contents = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        try:
            self._write_projection_bytes(job_id, contents)
        except DrawingMachineError:
            raise
        except (OSError, _secure_fs.SecureFilesystemError) as error:
            raise _translated(error, job_id=job_id) from error

    def _write_projection_bytes(self, job_id: str, contents: bytes) -> None:
        with _secure_fs.managed_jobs_root(self._paths.data_dir) as root, ExitStack() as stack:
            job_fd = _track(stack, _secure_fs.ensure_directory_at(root.jobs_fd, job_id))
            _secure_fs.cleanup_stale_projection_temps(job_fd)
            temporary_name = _secure_fs.hidden_name("job.json.tmp")
            temporary_fd = _track(stack, _secure_fs.create_regular_at(job_fd, temporary_name))
            try:
                _secure_fs.write_all(temporary_fd, contents)
                os.fchmod(temporary_fd, 0o600)
                _secure_fs.fsync_file(temporary_fd)
                _secure_fs.verify_entry_identity(job_fd, temporary_name, temporary_fd, directory=False)
                root.verify()
                _secure_fs.verify_entry_identity(root.jobs_fd, job_id, job_fd, directory=True)
                _secure_fs.replace_file(job_fd, temporary_name, "job.json")
                public_fd = _track(stack, _secure_fs.open_regular_at(job_fd, "job.json"))
                if _secure_fs.identity(os.fstat(public_fd)) != _secure_fs.identity(os.fstat(temporary_fd)):
                    raise artifact_error("projection public identity does not match the temporary file", job_id=job_id)
                public_metadata, public_contents = _secure_fs.inspect_descriptor(public_fd)
                expected_digest = hashlib.sha256(contents).hexdigest()
                if (
                    public_contents != contents
                    or public_metadata.sha256 != expected_digest
                    or public_metadata.size_bytes != len(contents)
                ):
                    raise artifact_error(
                        "projection public contents do not match the serialized payload", job_id=job_id
                    )
                _secure_fs.verify_entry_identity(job_fd, "job.json", public_fd, directory=False)
                _secure_fs.fsync_directory(job_fd)
                root.verify()
                _secure_fs.verify_entry_identity(root.jobs_fd, job_id, job_fd, directory=True)
            except BaseException:
                _secure_fs.remove_regular_if_identity(job_fd, temporary_name, temporary_fd)
                raise
