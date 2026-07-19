import hashlib
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib import resources

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

_MIGRATION_PACKAGE = "drawingmachine.adapters.persistence.migrations"
_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_MIGRATION_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sha256: str
    sql: str


def migrate(connection: sqlite3.Connection, *, applied_at: str) -> int:
    _bootstrap_ledger(connection)
    migrations = _load_migrations()
    _verify_migration_ledger(connection, migrations, require_complete=False)
    for migration in migrations:
        _apply_migration(connection, migration, applied_at=applied_at)
    return _verify_migration_ledger(connection, migrations, require_complete=True)


def _load_migrations() -> tuple[Migration, ...]:
    migrations: dict[int, Migration] = {}
    for resource in resources.files(_MIGRATION_PACKAGE).iterdir():
        if not resource.is_file() or resource.name == "__init__.py":
            continue
        match = _MIGRATION_NAME.fullmatch(resource.name)
        if match is None:
            if resource.name.endswith(".sql"):
                raise _migration_error(
                    code="DATABASE_MIGRATION_INVALID",
                    message=f"Invalid migration resource name: {resource.name}",
                    details={"migration_resource": resource.name},
                )
            continue
        contents = resource.read_bytes()
        try:
            sql = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _migration_error(
                code="DATABASE_MIGRATION_INVALID",
                message=f"Migration resource is not valid UTF-8: {resource.name}",
                details={"migration_resource": resource.name},
            ) from error
        version = int(match.group("version"))
        migration = Migration(
            version=version,
            name=match.group("name"),
            sha256=hashlib.sha256(contents).hexdigest(),
            sql=sql,
        )
        if version in migrations:
            raise _migration_error(
                code="DATABASE_MIGRATION_INVALID",
                message=f"Duplicate migration version: {version}",
                details={"migration_version": version},
            )
        migrations[version] = migration
    return tuple(migrations[version] for version in sorted(migrations))


def _bootstrap_ledger(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_MIGRATION_LEDGER_SQL)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _apply_migration(connection: sqlite3.Connection, migration: Migration, *, applied_at: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT name, sha256 FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if row is not None:
            _verify_applied_migration(migration, stored_name=str(row[0]), stored_sha256=str(row[1]))
        else:
            for statement in _migration_statements(migration):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, sha256, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.sha256, applied_at),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _verify_applied_migration(migration: Migration, *, stored_name: str, stored_sha256: str) -> None:
    details: JsonObject = {
        "migration_version": migration.version,
        "migration_name": migration.name,
    }
    if stored_name != migration.name:
        raise _migration_error(
            code="DATABASE_MIGRATION_IDENTITY_MISMATCH",
            message=f"Stored migration name does not match version {migration.version}",
            details=details,
        )
    if stored_sha256 != migration.sha256:
        raise _migration_error(
            code="DATABASE_MIGRATION_CHECKSUM_MISMATCH",
            message=f"Stored migration checksum does not match version {migration.version}",
            details=details,
        )


def _verify_migration_ledger(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    require_complete: bool,
) -> int:
    packaged_by_version = {migration.version: migration for migration in migrations}
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute("SELECT version, name, sha256 FROM schema_migrations ORDER BY version").fetchall()
        stored_versions: set[int] = set()
        for row in rows:
            version = int(row[0])
            stored_name = str(row[1])
            migration = packaged_by_version.get(version)
            if migration is None:
                raise _migration_error(
                    code="DATABASE_MIGRATION_UNKNOWN",
                    message=f"Stored migration has no packaged resource: {version}",
                    details={"migration_version": version, "migration_name": stored_name},
                )
            _verify_applied_migration(
                migration,
                stored_name=stored_name,
                stored_sha256=str(row[2]),
            )
            stored_versions.add(version)
        if require_complete:
            for migration in migrations:
                if migration.version not in stored_versions:
                    raise _migration_error(
                        code="DATABASE_MIGRATION_MISSING",
                        message=f"Packaged migration is missing from the ledger: {migration.version}",
                        details={
                            "migration_version": migration.version,
                            "migration_name": migration.name,
                        },
                    )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return max(stored_versions, default=0)


def _migration_statements(migration: Migration) -> Iterator[str]:
    start = 0
    for index, character in enumerate(migration.sql):
        if character != ";":
            continue
        candidate = migration.sql[start : index + 1]
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            start = index + 1
    remainder = migration.sql[start:].strip()
    if remainder:
        raise _migration_error(
            code="DATABASE_MIGRATION_INVALID",
            message=f"Migration SQL is incomplete: {migration.version:04d}_{migration.name}.sql",
            details={
                "migration_version": migration.version,
                "migration_name": migration.name,
            },
        )


def _migration_error(*, code: str, message: str, details: JsonObject) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.INTERNAL,
            message=message,
            retryable=False,
            details=details,
        )
    )
