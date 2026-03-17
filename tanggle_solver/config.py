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


@dataclass
class VpnConfig:
    """VPN rotation settings."""
    enabled: bool
    provider: str   # "openvpn", "pia", or "nordvpn"
    vpn_dir: str    # directory with .ovpn files (openvpn provider only)


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


def load_vpn_config(env_path: Optional[str] = None) -> Optional[VpnConfig]:
    """Load VPN settings from env vars or .env file.

    Env vars:
        VPN_ENABLED  — "true" or "false" (default: false)
        VPN_PROVIDER — "openvpn", "pia", or "nordvpn" (default: openvpn)
        VPN_DIR      — path to directory with .ovpn files (for openvpn provider)

    Returns None if VPN is not enabled.
    """
    load_env_file(env_path)

    enabled = os.environ.get("VPN_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    if not enabled:
        return None

    provider = os.environ.get("VPN_PROVIDER", "openvpn").strip().lower()
    vpn_dir = os.environ.get("VPN_DIR", "").strip()

    logger.info(f"VPN config loaded: provider={provider}, dir={vpn_dir or '(n/a)'}")
    return VpnConfig(enabled=True, provider=provider, vpn_dir=vpn_dir)