"""The instance directory, its settings file and its secrets.

    <instance>/                 a git repository (okg publishes only committed config)
        archi.yaml              settings: bundles, data root, runtime, ports (committed)
        archi.lock              what was rendered (committed; archi.bundles.lock)
        compose.yaml            the stack (committed)
        stack/mcp.Dockerfile    the MCP server image (committed)
        profiles/<name>/        the rendered okg profile (committed)
        deployments/<name>/     the okg deployment (committed)
        .env                    generated secrets, mode 0600 (git-ignored)
        .secrets/               okg's role-password file, mode 0600 (git-ignored)

The data root (connector caches, repository checkouts) is outside the
instance: `okg install --force` deletes everything inside a deployment, and
an instance is a git repository that should hold configuration, not data.
"""
from __future__ import annotations

import os
import secrets as _secrets
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

INSTANCE_FILE = "archi.yaml"
INSTANCE_VERSION = 1
ENV_FILE = ".env"
SECRETS_DIR = ".secrets"
ROLE_SECRETS_FILE = f"{SECRETS_DIR}/role-secrets.json"
COMPOSE_FILE = "compose.yaml"
MCP_DOCKERFILE = "stack/mcp.Dockerfile"
DEPLOYMENTS_DIR = "deployments"
GITIGNORE = (
    "# Written by `archi init`. Secrets never enter the repository.\n"
    f"{ENV_FILE}\n"
    f"{SECRETS_DIR}/\n"
)

#: Every secret `archi init` generates. Values are random; none is ever a
#: command-line argument or a committed file.
SECRET_KEYS = (
    "ARCHI_GRAPH_DB_PASSWORD",
    "ARCHI_CHAT_DB_PASSWORD",
    "OKG_CHAT_WEBUI_SECRET_KEY",
    "OKG_CHAT_MCP_TOKEN",
    "ARCHI_ADMIN_PASSWORD",
)


class InstanceError(Exception):
    """The directory is not a usable archi instance."""


@dataclass
class Ports:
    """Host ports, all published on 127.0.0.1 only."""

    graph_db: int
    mcp: int
    webui: int


@dataclass
class Instance:
    dir: Path
    name: str
    bundles: list[str]
    data_root: Path
    runtime: str
    ports: Ports
    with_packs: list[str] = field(default_factory=list)
    without_packs: list[str] = field(default_factory=list)
    profile: str = ""
    okg_version: str = ""
    admin_email: str = ""
    #: Per-automation settings, `{<name>: {level: L0|L1|L2}}`. The level an
    #: automation runs at; the bundle and the pack only bound it (ADR 0002
    #: section 7.3). L3 and unknown levels are refused when archi.yaml loads.
    automations: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def deployment_dir(self) -> Path:
        return self.dir / DEPLOYMENTS_DIR / self.name

    @property
    def project(self) -> str:
        """The compose project name: containers, volumes and network share it."""
        return f"archi-{self.name}"

    @property
    def database(self) -> str:
        return self.name.replace("-", "_")

    @property
    def env_path(self) -> Path:
        return self.dir / ENV_FILE

    @property
    def role_secrets_path(self) -> Path:
        return self.dir / ROLE_SECRETS_FILE

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("dir")
        data["data_root"] = str(self.data_root)
        if not data["automations"]:
            del data["automations"]
        return {"instance_version": INSTANCE_VERSION, **data}

    def save(self) -> Path:
        path = self.dir / INSTANCE_FILE
        path.write_text(
            "# archi.yaml: settings `archi init` chose. `archi up`, `down` and `doctor`\n"
            "# read it. Secrets are in .env, which git ignores.\n"
            + yaml.safe_dump(self.to_dict(), sort_keys=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, directory: Path) -> "Instance":
        directory = Path(directory).resolve()
        path = directory / INSTANCE_FILE
        if not path.is_file():
            raise InstanceError(
                f"{path} does not exist; run `archi init <name>` first, or pass "
                "--instance <dir> pointing at an instance"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if data.get("instance_version") != INSTANCE_VERSION:
            raise InstanceError(f"{path}: not an archi.yaml version {INSTANCE_VERSION}")
        return cls(
            dir=directory,
            name=data["name"],
            bundles=list(data["bundles"]),
            data_root=Path(data["data_root"]),
            runtime=data["runtime"],
            ports=Ports(**data["ports"]),
            with_packs=list(data.get("with_packs") or []),
            without_packs=list(data.get("without_packs") or []),
            profile=data.get("profile") or "",
            okg_version=data.get("okg_version") or "",
            admin_email=data.get("admin_email") or "",
            automations=_automation_settings(path, data.get("automations")),
        )


def _automation_settings(path: Path, raw: Any) -> dict[str, dict[str, str]]:
    from archi.ops.levels import settings_problems

    problems = settings_problems(raw, "level", "automations")
    if problems:
        raise InstanceError(f"{path}: " + "; ".join(problems))
    return {name: dict(body) for name, body in (raw or {}).items()}


def default_data_root(name: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "archi" / name


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_ports(count: int) -> list[int]:
    ports: list[int] = []
    while len(ports) < count:
        port = free_port()
        if port not in ports:
            ports.append(port)
    return ports


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


def generate_secrets() -> dict[str, str]:
    return {key: _secrets.token_urlsafe(32) for key in SECRET_KEYS}


def write_env(path: Path, values: Mapping[str, str]) -> None:
    """Write `values` as KEY=VALUE lines, readable only by the owner."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Generated by `archi init`. Never commit this file.\n")
        for key, value in values.items():
            fh.write(f"{key}={value}\n")
    os.chmod(path, 0o600)


def read_env(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        raise InstanceError(f"{path} does not exist; the instance has no secrets")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def graph_dsn(instance: Instance, secrets: Mapping[str, str], *, host: Optional[str] = None) -> str:
    """The superuser DSN for the graph database, from the host or the network."""
    if host is None:
        address = f"127.0.0.1:{instance.ports.graph_db}"
    else:
        address = f"{host}:5432"
    return (
        f"postgresql://postgres:{secrets['ARCHI_GRAPH_DB_PASSWORD']}@{address}/{instance.database}"
    )


def chat_dsn(secrets: Mapping[str, str]) -> str:
    """The Open WebUI app database, as the webui container reaches it."""
    return f"postgresql://postgres:{secrets['ARCHI_CHAT_DB_PASSWORD']}@chat-db:5432/okg_chat_app"


def host_env(instance: Instance, secrets: Mapping[str, str], base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment okg runs in on the host, for this instance.

    `OKG_PROFILES_DIR` is removed: okg is run from the instance directory and
    finds the rendered profile in `./profiles/`. Role passwords go to the
    instance's own `.secrets/` instead of okg's per-port file in ~/.config,
    which a second instance on a reused port would overwrite.
    """
    env = dict(os.environ if base is None else base)
    env.pop("OKG_PROFILES_DIR", None)
    env.update(
        OKG_DSN=graph_dsn(instance, secrets),
        OKG_DEPLOYMENTS_DIR=str(instance.dir / DEPLOYMENTS_DIR),
        OKG_ROLE_SECRETS_FILE=str(instance.role_secrets_path),
        ARCHI_DATA_ROOT=str(instance.data_root),
        OKG_CHAT_MCP_TOKEN=secrets["OKG_CHAT_MCP_TOKEN"],
        OKG_CHAT_WEBUI_SECRET_KEY=secrets["OKG_CHAT_WEBUI_SECRET_KEY"],
        OKG_CHAT_APP_DATABASE_URL=chat_dsn(secrets),
    )
    return env
