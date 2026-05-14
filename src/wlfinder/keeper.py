"""Retaining a successful server.

On a hit the server is kept running (never deleted) and we build an SSH
access hint for the admin. Nothing is provisioned on the box itself — the
result is delivered via :mod:`wlfinder.notifier`.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import BaseModel

from wlfinder.models import CreatedServer

log = structlog.get_logger(__name__)

DEFAULT_KEY_PATH = Path.home() / ".ssh" / "wlfinder"


class SshKeyPair(BaseModel):
    private_path: Path
    public_path: Path
    public: str  # OpenSSH-format public key line


def ensure_local_ssh_key(path: Path = DEFAULT_KEY_PATH) -> SshKeyPair:
    """Return an ed25519 keypair at *path*, generating one if it is absent."""
    path = Path(path).expanduser()
    pub_path = path.with_name(path.name + ".pub")
    if path.exists() and pub_path.exists():
        return SshKeyPair(
            private_path=path,
            public_path=pub_path,
            public=pub_path.read_text(encoding="utf-8").strip(),
        )

    key = ed25519.Ed25519PrivateKey.generate()
    priv_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    public = f"{pub_bytes.decode()} wlfinder"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(priv_bytes)
    path.chmod(0o600)
    pub_path.write_text(public + "\n", encoding="utf-8")
    log.info("ssh.key_generated", path=str(path))
    return SshKeyPair(private_path=path, public_path=pub_path, public=public)


class KeptServer(BaseModel):
    """A winning server we have decided to keep running."""

    server: CreatedServer
    ssh_command: str


def keep_server(server: CreatedServer, ssh_key: SshKeyPair) -> KeptServer:
    """Phase 1 'keeper': retain the server and build an SSH access hint."""
    ssh_command = (
        f"ssh -i {ssh_key.private_path} "
        f"-o StrictHostKeyChecking=accept-new root@{server.public_ipv4}"
    )
    log.info(
        "keeper.kept",
        hoster=server.hoster,
        server_id=server.server_id,
        ipv4=server.public_ipv4,
    )
    return KeptServer(server=server, ssh_command=ssh_command)
