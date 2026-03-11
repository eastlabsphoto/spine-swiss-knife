"""Auto-update from GitHub main branch for Spine Swiss Knife."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal

from . import __version__

_REPO = "eastlabsphoto/spine-swiss-knife"
_RAW_INIT = f"https://raw.githubusercontent.com/{_REPO}/main/spine_swiss_knife/__init__.py"
_ZIP_URL = f"https://github.com/{_REPO}/archive/refs/heads/main.zip"
_COMMITS_URL = f"https://api.github.com/repos/{_REPO}/commits?per_page=10"
_RELEASES_URL = f"https://github.com/{_REPO}/releases/latest"
_APP_DIR = Path(__file__).parent

IS_FROZEN = getattr(sys, "frozen", False)

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '0.1.2' into (0, 1, 2)."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


class UpdateChecker(QThread):
    """Background thread — checks __version__ on main branch vs local."""

    update_available = Signal(str, str, str)  # version, zip_url, changelog

    def run(self):
        try:
            # Fetch remote __init__.py to get version
            with urlopen(_RAW_INIT, timeout=10) as resp:
                init_src = resp.read().decode()

            match = _VERSION_RE.search(init_src)
            if not match:
                return

            remote_ver = match.group(1)
            if _parse_version(remote_ver) <= _parse_version(__version__):
                return

            # Fetch recent commit messages as changelog
            changelog = self._fetch_changelog()

            self.update_available.emit(remote_ver, _ZIP_URL, changelog)

        except (URLError, OSError, ValueError, KeyError):
            pass  # silent fail

    def _fetch_changelog(self) -> str:
        try:
            req = Request(_COMMITS_URL, headers={"Accept": "application/vnd.github.v3+json"})
            with urlopen(req, timeout=10) as resp:
                commits = json.loads(resp.read().decode())
            lines = []
            for c in commits[:5]:
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                if msg:
                    lines.append(f"• {msg}")
            return "\n".join(lines)
        except Exception:
            return ""


def perform_update(zip_url: str) -> None:
    """Download main branch ZIP, extract, replace spine_swiss_knife/ contents.

    For frozen builds (PyInstaller .exe/.app), opens browser to releases page instead.
    """
    if IS_FROZEN:
        webbrowser.open(_RELEASES_URL)
        return

    tmp_dir = tempfile.mkdtemp(prefix="ssk_update_")
    zip_path = os.path.join(tmp_dir, "release.zip")

    try:
        with urlopen(zip_url, timeout=60) as resp:
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # ZIP has one root dir: spine-swiss-knife-main/
        roots = os.listdir(extract_dir)
        if len(roots) != 1:
            raise RuntimeError("Unexpected ZIP structure")

        source_pkg = Path(extract_dir) / roots[0] / "spine_swiss_knife"
        if not source_pkg.is_dir():
            raise RuntimeError("spine_swiss_knife/ not found in archive")

        # Replace files (skip __pycache__)
        for item in source_pkg.rglob("*"):
            if "__pycache__" in item.parts:
                continue
            rel = item.relative_to(source_pkg)
            dest = _APP_DIR / rel
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(dest))

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def restart_app() -> None:
    """Restart the application by launching a new process and exiting.

    For frozen builds, just exits (user already got the releases page).
    """
    if IS_FROZEN:
        sys.exit(0)
    subprocess.Popen([sys.executable] + sys.argv)
    sys.exit(0)
