"""Settings and paths. Everything can be overridden with environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APPROVAL_LIMIT_EUR = float(os.getenv("APPROVAL_LIMIT_EUR", "10000"))  # above this a person must approve
PRICE_TOLERANCE = float(os.getenv("PRICE_TOLERANCE", "0.02"))  # 2% above the PO price is still fine
TOKEN_TTL_S = int(os.getenv("TOKEN_TTL_S", "900"))  # an approval token is valid for 15 minutes
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))
APP_URL = os.getenv("APP_URL", "").rstrip("/")  # used for links in Teams cards


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("IG_DATA_DIR", ROOT / "data")))
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))  # or "bedrock"
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "eu-central-1"))
    agent_model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", "claude-sonnet-4-6"))
    writer_model: str = field(default_factory=lambda: os.getenv("WRITER_MODEL", "claude-haiku-4-5"))

    @property
    def erp_db(self) -> Path:
        return self.data_dir / "erp.db"

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def cases_db(self) -> Path:
        return self.data_dir / "cases.db"

    @property
    def audit_db(self) -> Path:
        return self.data_dir / "audit.db"

    @property
    def labels(self) -> Path:
        """Expected outcomes for the evals. Never served to the agent."""
        return self.data_dir / "labels.json"


def approval_secret(data_dir: Path) -> str:
    """Key that signs human approvals. From the environment, or created once per data folder."""
    if os.getenv("APPROVAL_SECRET"):
        return os.environ["APPROVAL_SECRET"]
    path = Path(data_dir) / ".approval_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()
