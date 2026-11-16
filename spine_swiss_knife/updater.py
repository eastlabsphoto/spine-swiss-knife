"""Update check against GitHub for Spine Swiss Knife.

Only *checks* for a newer version; the welcome page opens the releases page
in the browser (no in-app download or file replacement).
"""

from __future__ import annotations

import json
import platform
import re
import ssl
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal

from . import __version__

_REPO = "eastlabsphoto/spine-swiss-knife"
_RAW_INIT = f"https://raw.githubusercontent.com/{_REPO}/main/spine_swiss_knife/__init__.py"
_ZIP_URL = f"https://github.com/{_REPO}/archive/refs/heads/main.zip"
_COMMITS_URL = f"https://api.github.com/repos/{_REPO}/commits?per_page=10"
_RELEASE_API = f"https://api.github.com/repos/{_REPO}/releases/latest"

IS_FROZEN = getattr(sys, "frozen", False)

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

# SSL context — certifi for frozen builds (PyInstaller doesn't bundle system certs)
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def _urlopen(url, timeout=10):
    """urlopen wrapper that uses proper SSL context."""
    if isinstance(url, str):
        url = Request(url)
    return urlopen(url, timeout=timeout, context=_SSL_CTX)


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '0.1.2' into (0, 1, 2)."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


def _platform_release_asset_name(system_name: str | None = None) -> str:
    """Return the expected frozen release asset name for the current platform."""
    if (system_name or platform.system()) == "Darwin":
        return "SpineSwissKnife-macOS.zip"
    return "SpineSwissKnife-Windows.zip"


def _find_release_asset_url(release: dict, system_name: str | None = None) -> str:
    """Return browser_download_url for the platform-specific release asset."""
    asset_name = _platform_release_asset_name(system_name)
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            url = asset.get("browser_download_url", "")
            if url:
                return url
    raise KeyError(f"Release asset not found: {asset_name}")


def _fetch_latest_release() -> dict:
    """Fetch GitHub Releases latest JSON payload."""
    req = Request(_RELEASE_API, headers={"Accept": "application/vnd.github.v3+json"})
    with _urlopen(req) as resp:
        return json.loads(resp.read().decode())


class UpdateChecker(QThread):
    """Background thread — checks __version__ on main branch vs local."""

    update_available = Signal(str, str, str)  # version, download_url, changelog

    def run(self):
        try:
            if IS_FROZEN:
                release = _fetch_latest_release()
                remote_ver = release.get("tag_name", "").lstrip("v")
                if not remote_ver:
                    return
                if _parse_version(remote_ver) <= _parse_version(__version__):
                    return
                url = _find_release_asset_url(release)
                changelog = release.get("body", "").strip() or self._fetch_changelog()
            else:
                # Fetch remote __init__.py to get version
                with _urlopen(_RAW_INIT) as resp:
                    init_src = resp.read().decode()

                match = _VERSION_RE.search(init_src)
                if not match:
                    return

                remote_ver = match.group(1)
                if _parse_version(remote_ver) <= _parse_version(__version__):
                    return

                url = _ZIP_URL
                changelog = self._fetch_changelog()

            self.update_available.emit(remote_ver, url, changelog)

        except (URLError, OSError, ValueError, KeyError):
            pass  # silent fail

    def _fetch_changelog(self) -> str:
        try:
            req = Request(_COMMITS_URL, headers={"Accept": "application/vnd.github.v3+json"})
            with _urlopen(req) as resp:
                commits = json.loads(resp.read().decode())
            lines = []
            for c in commits[:5]:
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                if msg:
                    lines.append(f"• {msg}")
            return "\n".join(lines)
        except Exception:
            return ""
