"""Database connection helper.

Credentials are never hardcoded:
- master: the RDS instance's own managed secret, found through ``MasterUserSecret.SecretArn``
- app roles: the ``akinsds1126-expense-db-app`` secret (created by ``expense_agent.db.app_roles``)

TLS is always ``verify-full`` against the committed public RDS CA bundle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    import boto3
    import psycopg

Role = Literal["master", "readonly", "writer", "ml", "ingest", "memory"]
AppRole = Literal["readonly", "writer", "ml", "ingest", "memory"]

APP_ROLE_NAMES: dict[AppRole, str] = {
    "readonly": "app_readonly",
    "writer": "app_writer",
    "ml": "app_ml",
    "ingest": "app_ingest",
    "memory": "app_memory",
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CA_BUNDLE = _REPO_ROOT / "certs" / "rds-global-bundle.pem"
DEFAULT_PROFILE = "aero"


class SecretNotFoundError(RuntimeError):
    """Raised when a required Secrets Manager secret does not exist."""


@dataclass(frozen=True)
class DbSettings:
    """Where the database is. Overridable by environment variables (no secrets here)."""

    instance_id: str = "akinsds1126-expense-db"
    dbname: str = "expense"
    region: str = "eu-west-2"
    app_secret_id: str = "akinsds1126-expense-db-app"  # noqa: S105 - secret *name*, not a value
    ml_secret_id: str = "akinsds1126-expense-db-ml"  # noqa: S105 - secret *name*, not a value
    ingest_secret_id: str = "akinsds1126-expense-db-ingest"  # noqa: S105 - a *name*
    memory_secret_id: str = "akinsds1126-expense-db-memory"  # noqa: S105 - a *name*
    ca_bundle: Path = DEFAULT_CA_BUNDLE
    connect_timeout: int = 10

    def secret_for(self, role: AppRole) -> str:
        """Each secret holds a set of roles; app_ml has its own so the trainer can't read others."""
        if role == "ml":
            return self.ml_secret_id
        if role == "ingest":
            return self.ingest_secret_id
        if role == "memory":
            return self.memory_secret_id
        return self.app_secret_id

    def roles_by_secret(self) -> dict[str, tuple[str, ...]]:
        return {
            self.app_secret_id: (APP_ROLE_NAMES["readonly"], APP_ROLE_NAMES["writer"]),
            self.ml_secret_id: (APP_ROLE_NAMES["ml"],),
            self.ingest_secret_id: (APP_ROLE_NAMES["ingest"],),
            self.memory_secret_id: (APP_ROLE_NAMES["memory"],),
        }

    @classmethod
    def from_env(cls) -> DbSettings:
        d = cls()
        return cls(
            instance_id=os.environ.get("EXPENSE_DB_INSTANCE_ID", d.instance_id),
            dbname=os.environ.get("EXPENSE_DB_NAME", d.dbname),
            region=os.environ.get("AWS_REGION", d.region),
            app_secret_id=os.environ.get("EXPENSE_DB_APP_SECRET_ID", d.app_secret_id),
            ml_secret_id=os.environ.get("EXPENSE_DB_ML_SECRET_ID", d.ml_secret_id),
            ingest_secret_id=os.environ.get("EXPENSE_DB_INGEST_SECRET_ID", d.ingest_secret_id),
            memory_secret_id=os.environ.get("EXPENSE_DB_MEMORY_SECRET_ID", d.memory_secret_id),
            ca_bundle=Path(os.environ.get("EXPENSE_DB_CA_BUNDLE", str(d.ca_bundle))),
        )


@dataclass(frozen=True)
class DbCredentials:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"DbCredentials(host={self.host!r}, port={self.port}, dbname={self.dbname!r}, "
            f"user={self.user!r}, password='***')"
        )


@dataclass(frozen=True)
class InstanceInfo:
    host: str
    port: int
    master_secret_arn: str


def make_session(profile: str | None, region: str) -> boto3.Session:
    """A boto3 session. ``profile=None`` uses the default chain (e.g. the ECS task role)."""
    import boto3

    return boto3.Session(profile_name=profile or None, region_name=region)


def describe_instance(session: Any, instance_id: str) -> InstanceInfo:
    """Read the endpoint and the RDS-managed master secret ARN from the instance itself."""
    rds = session.client("rds")
    instances = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"]
    if not instances:
        raise RuntimeError(f"RDS instance {instance_id!r} not found")
    inst = instances[0]
    secret = inst.get("MasterUserSecret") or {}
    arn = secret.get("SecretArn")
    if not arn:
        raise RuntimeError(
            f"RDS instance {instance_id!r} has no MasterUserSecret; "
            "master credentials must be managed by RDS in Secrets Manager"
        )
    return InstanceInfo(
        host=inst["Endpoint"]["Address"], port=int(inst["Endpoint"]["Port"]), master_secret_arn=arn
    )


def get_secret_json(session: Any, secret_id: str) -> dict[str, Any]:
    client = session.client("secretsmanager")
    try:
        value = client.get_secret_value(SecretId=secret_id)
    except client.exceptions.ResourceNotFoundException as exc:
        raise SecretNotFoundError(f"secret {secret_id!r} does not exist") from exc
    data: dict[str, Any] = json.loads(value["SecretString"])
    return data


def get_master_credentials(session: Any, settings: DbSettings) -> DbCredentials:
    info = describe_instance(session, settings.instance_id)
    secret = get_secret_json(session, info.master_secret_arn)
    return DbCredentials(
        host=info.host,
        port=info.port,
        dbname=settings.dbname,
        user=secret["username"],
        password=secret["password"],
    )


def get_app_credentials(session: Any, settings: DbSettings, role: AppRole) -> DbCredentials:
    secret = get_secret_json(session, settings.secret_for(role))
    entry = secret[APP_ROLE_NAMES[role]]
    return DbCredentials(
        host=secret["host"],
        port=int(secret["port"]),
        dbname=secret["dbname"],
        user=entry["username"],
        password=entry["password"],
    )


def connect_kwargs(creds: DbCredentials, settings: DbSettings) -> dict[str, Any]:
    """Keyword arguments for ``psycopg.connect``. The password is passed as its own
    argument, never inside a DSN string that could end up in a log."""
    if not settings.ca_bundle.is_file():
        raise FileNotFoundError(f"RDS CA bundle not found at {settings.ca_bundle}")
    return {
        "host": creds.host,
        "port": creds.port,
        "dbname": creds.dbname,
        "user": creds.user,
        "password": creds.password,
        "sslmode": "verify-full",
        "sslrootcert": str(settings.ca_bundle),
        "connect_timeout": settings.connect_timeout,
        "application_name": "expense-agent",
    }


def get_credentials(session: Any, settings: DbSettings, role: Role) -> DbCredentials:
    if role == "master":
        return get_master_credentials(session, settings)
    return get_app_credentials(session, settings, role)


def connect(
    role: Role = "readonly",
    profile: str | None = None,
    settings: DbSettings | None = None,
    *,
    autocommit: bool = False,
) -> psycopg.Connection[Any]:
    """Open a TLS (verify-full) connection as the given role."""
    import psycopg

    settings = settings or DbSettings.from_env()
    session = make_session(profile, settings.region)
    creds = get_credentials(session, settings, role)
    return psycopg.connect(**connect_kwargs(creds, settings), autocommit=autocommit)


def add_profile_argument(parser: Any) -> None:
    """Shared ``--profile`` CLI option (pass an empty string to use the default AWS chain)."""
    parser.add_argument(
        "--profile",
        default=os.environ.get("EXPENSE_AWS_PROFILE", DEFAULT_PROFILE),
        help="AWS profile (default: aero; '' = default credential chain, e.g. ECS task role)",
    )
