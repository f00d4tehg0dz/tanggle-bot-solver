"""Configuration loader for tanggle.io credentials.

Reads from .env file or environment variables.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TanggleCredentials:
    """Login credentials for tanggle.io."""
    email: str
    password: str


def load_env_file(env_path: Optional[str] = None) -> None:
    """Load variables from a .env file into os.environ."""
    if env_path is None:
        # Walk up from CWD looking for .env
        candidates = [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent.parent / ".env",
        ]
    else:
        candidates = [Path(env_path)]

    for path in candidates:
        if path.is_file():
            logger.info(f"Loading env from {path}")
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("\"'")
                    os.environ.setdefault(key, value)
            return

    logger.debug("No .env file found")


def load_credentials(env_path: Optional[str] = None) -> Optional[TanggleCredentials]:
    """Load tanggle.io credentials from env vars or .env file.

    Returns None if credentials are not configured.
    """
    load_env_file(env_path)

    email = os.environ.get("TANGGLE_EMAIL", "").strip()
    password = os.environ.get("TANGGLE_PASSWORD", "").strip()

    if email and password:
        logger.info(f"Credentials loaded for {email}")
        return TanggleCredentials(email=email, password=password)

    logger.debug("No credentials configured")
    return None