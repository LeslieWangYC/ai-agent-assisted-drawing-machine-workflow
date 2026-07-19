from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class RequestContext:
    peer: PeerCredentials
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    version: str
    protocol_version: int
    database_schema_version: int
    service_epoch: str
    started_at: datetime
    pid: int
