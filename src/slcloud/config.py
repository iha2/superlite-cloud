"""Settings loader."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    data_dir: str
    db_path: str
    work_dir: str
    master_key: str
    public_url: str = "http://localhost:8787"
    sandbox: str = "docker"
    worker_image: str = "slcloud-worker:0.1.0"
    gvisor: bool = False


def load_settings(env: dict[str, str]) -> Settings:
    key = env.get("SLCLOUD_MASTER_KEY", "")
    if not key:
        raise ValueError("SLCLOUD_MASTER_KEY is required")
    data = env.get("SLCLOUD_DATA", "/data")
    sandbox = env.get("SLCLOUD_SANDBOX", "docker")
    if sandbox not in ("docker", "process"):
        raise ValueError("SLCLOUD_SANDBOX must be docker or process")
    return Settings(
        data_dir=data,
        db_path=f"{data}/slcloud.db",
        work_dir=env.get("SLCLOUD_WORK") or f"{data}/work",
        master_key=key,
        public_url=env.get("SLCLOUD_URL", "http://localhost:8787"),
        sandbox=sandbox,
        worker_image=env.get("SLCLOUD_WORKER_IMAGE", "slcloud-worker:0.1.0"),
        gvisor=env.get("SLCLOUD_GVISOR", "") in ("1", "true", "yes"),
    )
