"""VPN connection manager with automatic rotation.

Supports three providers:
  - openvpn : raw .ovpn config files in a directory
  - pia     : Private Internet Access CLI (piactl)
  - nordvpn : NordVPN CLI (nordvpn)

Cycles through servers/configs when the current IP gets blocked (HTTP 403).
"""

import asyncio
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Region lists for CLI-based providers ─────────────────────────────────────

# PIA regions — a broad selection for rotation. Full list via `piactl get regions`.
PIA_REGIONS = [
    "us-east", "us-atlanta", "us-chicago", "us-denver", "us-houston",
    "us-las-vegas", "us-new-york", "us-seattle", "us-silicon-valley",
    "us-washington-dc", "us-west", "ca-montreal", "ca-toronto", "ca-vancouver",
    "uk-london", "uk-manchester", "de-berlin", "de-frankfurt",
    "nl-amsterdam", "se-stockholm", "ch-zurich", "fr-paris",
    "au-melbourne", "au-sydney", "jp-tokyo", "sg-singapore",
]

# NordVPN countries/servers for rotation. Full list via `nordvpn countries`.
NORD_SERVERS = [
    "United_States", "Canada", "United_Kingdom", "Germany",
    "Netherlands", "Sweden", "Switzerland", "France",
    "Australia", "Japan", "Singapore", "Ireland",
    "Belgium", "Denmark", "Norway", "Poland",
    "Czech_Republic", "Romania", "Spain", "Italy",
]


# ── Base class ───────────────────────────────────────────────────────────────

class VpnProvider(ABC):
    """Abstract VPN provider interface."""

    def __init__(self):
        self._current_index: int = -1

    @property
    @abstractmethod
    def total_servers(self) -> int: ...

    @property
    def has_configs(self) -> bool:
        return self.total_servers > 0

    @property
    def configs_remaining(self) -> int:
        if self.total_servers == 0:
            return 0
        return self.total_servers - (self._current_index + 1)

    @property
    @abstractmethod
    def current_config(self) -> Optional[str]: ...

    @abstractmethod
    async def connect_next(self) -> bool:
        """Disconnect and connect to the next server. Returns False when exhausted."""
        ...

    @abstractmethod
    async def disconnect(self): ...

    async def cleanup(self):
        await self.disconnect()


# ── OpenVPN (.ovpn files) ────────────────────────────────────────────────────

class OpenVpnProvider(VpnProvider):
    """Manages raw OpenVPN connections via .ovpn config files."""

    def __init__(self, vpn_dir: str, openvpn_exe: Optional[str] = None):
        super().__init__()
        self.vpn_dir = Path(vpn_dir)
        self.openvpn_exe = openvpn_exe or self._find_openvpn()
        self._configs: list[Path] = []
        self._process: Optional[subprocess.Popen] = None
        self._load_configs()

    @property
    def total_servers(self) -> int:
        return len(self._configs)

    @property
    def current_config(self) -> Optional[str]:
        if 0 <= self._current_index < len(self._configs):
            return self._configs[self._current_index].name
        return None

    def _find_openvpn(self) -> str:
        candidates = [
            r"C:\Program Files\OpenVPN\bin\openvpn.exe",
            r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return "openvpn"

    def _load_configs(self):
        if not self.vpn_dir.is_dir():
            logger.warning(f"VPN directory not found: {self.vpn_dir}")
            return
        self._configs = sorted(self.vpn_dir.glob("*.ovpn"))
        if not self._configs:
            logger.warning(f"No .ovpn files found in {self.vpn_dir}")
        else:
            logger.info(f"Loaded {len(self._configs)} OpenVPN configs from {self.vpn_dir}")
            for c in self._configs:
                logger.debug(f"  {c.name}")

    async def connect_next(self) -> bool:
        await self.disconnect()

        self._current_index += 1
        if self._current_index >= len(self._configs):
            logger.error("No more OpenVPN configs to try — all exhausted")
            return False

        config_path = self._configs[self._current_index]
        logger.info(
            f"Connecting to OpenVPN [{self._current_index + 1}/{len(self._configs)}]: "
            f"{config_path.name}"
        )

        try:
            self._process = subprocess.Popen(
                [self.openvpn_exe, "--config", str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            connected = await self._wait_for_connection(timeout=30)
            if connected:
                logger.info(f"OpenVPN connected via {config_path.name}")
                return True
            else:
                logger.warning(f"OpenVPN connection timed out for {config_path.name}")
                await self.disconnect()
                return await self.connect_next()

        except FileNotFoundError:
            logger.error(
                f"OpenVPN executable not found: {self.openvpn_exe}\n"
                "Install OpenVPN or set OPENVPN_EXE in your .env"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to start OpenVPN: {e}")
            await self.disconnect()
            return await self.connect_next()

    async def _wait_for_connection(self, timeout: int = 30) -> bool:
        if not self._process or not self._process.stdout:
            return False

        start = time.time()
        loop = asyncio.get_event_loop()

        while time.time() - start < timeout:
            line = await loop.run_in_executor(None, self._read_line_safe)
            if line is None:
                return False
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str:
                logger.debug(f"[openvpn] {line_str}")

            if "Initialization Sequence Completed" in line_str:
                return True
            if "AUTH_FAILED" in line_str:
                logger.error("OpenVPN authentication failed")
                return False
            if "Connection refused" in line_str or "SIGTERM" in line_str:
                return False

            await asyncio.sleep(0.1)

        return False

    def _read_line_safe(self) -> Optional[bytes]:
        if not self._process or not self._process.stdout:
            return None
        try:
            if self._process.poll() is not None:
                return None
            return self._process.stdout.readline()
        except Exception:
            return None

    async def disconnect(self):
        if self._process:
            logger.info("Disconnecting OpenVPN...")
            try:
                self._process.terminate()
                await asyncio.sleep(2)
                if self._process.poll() is None:
                    self._process.kill()
            except Exception as e:
                logger.debug(f"OpenVPN disconnect error (non-fatal): {e}")
            self._process = None
            await asyncio.sleep(1)


# ── PIA (Private Internet Access) via piactl ─────────────────────────────────

class PiaProvider(VpnProvider):
    """Manages PIA connections via the piactl CLI.

    Requires PIA desktop app installed with the CLI enabled.
    Windows: C:\\Program Files\\Private Internet Access\\piactl.exe
    """

    def __init__(self, regions: Optional[list[str]] = None):
        super().__init__()
        self._exe = self._find_piactl()
        self._regions = regions or PIA_REGIONS

    @property
    def total_servers(self) -> int:
        return len(self._regions)

    @property
    def current_config(self) -> Optional[str]:
        if 0 <= self._current_index < len(self._regions):
            return f"pia:{self._regions[self._current_index]}"
        return None

    def _find_piactl(self) -> str:
        candidates = [
            r"C:\Program Files\Private Internet Access\piactl.exe",
            r"C:\Program Files (x86)\Private Internet Access\piactl.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return "piactl"

    async def _run(self, *args: str, timeout: int = 15) -> tuple[int, str]:
        """Run a piactl command and return (returncode, stdout)."""
        proc = await asyncio.create_subprocess_exec(
            self._exe, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return proc.returncode, stdout.decode("utf-8", errors="replace").strip()

    async def connect_next(self) -> bool:
        await self.disconnect()

        self._current_index += 1
        if self._current_index >= len(self._regions):
            logger.error("No more PIA regions to try — all exhausted")
            return False

        region = self._regions[self._current_index]
        logger.info(
            f"Connecting to PIA [{self._current_index + 1}/{len(self._regions)}]: {region}"
        )

        # Set the region
        rc, out = await self._run("set", "region", region)
        if rc != 0:
            logger.warning(f"piactl set region failed: {out}")
            return await self.connect_next()

        # Connect
        rc, out = await self._run("connect")
        if rc != 0:
            logger.warning(f"piactl connect failed: {out}")
            return await self.connect_next()

        # Poll connection state
        connected = await self._wait_connected(timeout=30)
        if connected:
            logger.info(f"PIA connected to {region}")
            return True
        else:
            logger.warning(f"PIA connection to {region} timed out")
            await self.disconnect()
            return await self.connect_next()

    async def _wait_connected(self, timeout: int = 30) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            rc, state = await self._run("get", "connectionstate")
            if state == "Connected":
                return True
            if state in ("Disconnected", "DisconnectedError"):
                return False
            await asyncio.sleep(1)
        return False

    async def disconnect(self):
        logger.info("Disconnecting PIA...")
        await self._run("disconnect")
        # Wait for disconnect to settle
        start = time.time()
        while time.time() - start < 10:
            rc, state = await self._run("get", "connectionstate")
            if state == "Disconnected":
                break
            await asyncio.sleep(1)
        await asyncio.sleep(1)


# ── NordVPN via nordvpn CLI ──────────────────────────────────────────────────

class NordVpnProvider(VpnProvider):
    """Manages NordVPN connections via the nordvpn CLI.

    Requires the NordVPN desktop app installed.
    Windows: C:\\Program Files\\NordVPN\\nordvpn.exe
    """

    def __init__(self, servers: Optional[list[str]] = None):
        super().__init__()
        self._exe = self._find_nordvpn()
        self._servers = servers or NORD_SERVERS

    @property
    def total_servers(self) -> int:
        return len(self._servers)

    @property
    def current_config(self) -> Optional[str]:
        if 0 <= self._current_index < len(self._servers):
            return f"nord:{self._servers[self._current_index]}"
        return None

    def _find_nordvpn(self) -> str:
        candidates = [
            r"C:\Program Files\NordVPN\nordvpn.exe",
            r"C:\Program Files (x86)\NordVPN\nordvpn.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return "nordvpn"

    async def _run(self, *args: str, timeout: int = 30) -> tuple[int, str]:
        """Run a nordvpn command and return (returncode, stdout)."""
        proc = await asyncio.create_subprocess_exec(
            self._exe, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return proc.returncode, stdout.decode("utf-8", errors="replace").strip()

    async def connect_next(self) -> bool:
        await self.disconnect()

        self._current_index += 1
        if self._current_index >= len(self._servers):
            logger.error("No more NordVPN servers to try — all exhausted")
            return False

        server = self._servers[self._current_index]
        logger.info(
            f"Connecting to NordVPN [{self._current_index + 1}/{len(self._servers)}]: {server}"
        )

        # NordVPN CLI: `nordvpn connect <country_or_server>`
        # On Windows the flag syntax is: nordvpn -c -g <server>
        if os.name == "nt":
            rc, out = await self._run("-c", "-g", server)
        else:
            rc, out = await self._run("connect", server)

        logger.debug(f"[nordvpn] {out}")

        if rc != 0 or "error" in out.lower():
            logger.warning(f"NordVPN connect to {server} failed: {out}")
            return await self.connect_next()

        # Verify connection
        connected = await self._wait_connected(timeout=30)
        if connected:
            logger.info(f"NordVPN connected to {server}")
            return True
        else:
            logger.warning(f"NordVPN connection to {server} timed out")
            await self.disconnect()
            return await self.connect_next()

    async def _wait_connected(self, timeout: int = 30) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if os.name == "nt":
                rc, out = await self._run("-s")
            else:
                rc, out = await self._run("status")
            if "Connected" in out or "Status: Connected" in out:
                return True
            if "Disconnected" in out:
                return False
            await asyncio.sleep(1)
        return False

    async def disconnect(self):
        logger.info("Disconnecting NordVPN...")
        if os.name == "nt":
            await self._run("-d")
        else:
            await self._run("disconnect")
        await asyncio.sleep(2)


# ── Factory ──────────────────────────────────────────────────────────────────

def create_vpn(provider: str, vpn_dir: Optional[str] = None) -> VpnProvider:
    """Create a VPN provider by name.

    Args:
        provider: One of "openvpn", "pia", "nordvpn".
        vpn_dir: Directory with .ovpn files (required for "openvpn" provider).

    Returns:
        A configured VpnProvider instance.
    """
    provider = provider.lower().strip()

    if provider == "openvpn":
        if not vpn_dir:
            raise ValueError("--vpn-dir is required when using the openvpn provider")
        return OpenVpnProvider(vpn_dir)
    elif provider == "pia":
        return PiaProvider()
    elif provider == "nordvpn":
        return NordVpnProvider()
    else:
        raise ValueError(
            f"Unknown VPN provider: {provider!r}. "
            f"Supported: openvpn, pia, nordvpn"
        )


# Backward-compatible alias
VpnManager = OpenVpnProvider