"""Auto-update from GitHub main branch for Spine Swiss Knife."""

import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal, QObject

from . import __version__

_REPO = "eastlabsphoto/spine-swiss-knife"
_RAW_INIT = f"https://raw.githubusercontent.com/{_REPO}/main/spine_swiss_knife/__init__.py"
_ZIP_URL = f"https://github.com/{_REPO}/archive/refs/heads/main.zip"
_COMMITS_URL = f"https://api.github.com/repos/{_REPO}/commits?per_page=10"
_RELEASE_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_APP_DIR = Path(__file__).parent

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


def _get_frozen_download_url(version: str) -> str:
    """Get the platform-specific download URL from GitHub Releases."""
    if platform.system() == "Darwin":
        asset_name = "SpineSwissKnife-macOS.zip"
    else:
        asset_name = "SpineSwissKnife-Windows.zip"
    return f"https://github.com/{_REPO}/releases/download/v{version}/{asset_name}"


class UpdateChecker(QThread):
    """Background thread — checks __version__ on main branch vs local."""

    update_available = Signal(str, str, str)  # version, download_url, changelog

    def run(self):
        try:
            # Fetch remote __init__.py to get version
            with _urlopen(_RAW_INIT) as resp:
                init_src = resp.read().decode()

            match = _VERSION_RE.search(init_src)
            if not match:
                return

            remote_ver = match.group(1)
            if _parse_version(remote_ver) <= _parse_version(__version__):
                return

            changelog = self._fetch_changelog()

            # For frozen builds, point to platform-specific release asset
            if IS_FROZEN:
                url = _get_frozen_download_url(remote_ver)
            else:
                url = _ZIP_URL

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


class UpdateWorker(QThread):
    """Background thread for downloading and installing updates."""

    finished = Signal()        # success
    failed = Signal(str)       # error message
    progress = Signal(str)     # status text for UI

    def __init__(self, download_url: str, parent=None):
        super().__init__(parent)
        self._url = download_url

    def run(self):
        try:
            self.progress.emit("Downloading update...")
            perform_update(self._url)
            self.finished.emit()
        except Exception as e:
            self.failed.emit(str(e))


def perform_update(download_url: str) -> None:
    """Download update and replace app files.

    Source builds: download main branch ZIP, replace spine_swiss_knife/ package.
    Frozen builds: download platform ZIP from release, replace app contents.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ssk_update_")
    zip_path = os.path.join(tmp_dir, "update.zip")

    try:
        with _urlopen(download_url, timeout=120) as resp:
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        roots = os.listdir(extract_dir)
        if len(roots) != 1:
            raise RuntimeError("Unexpected ZIP structure")

        extracted_root = Path(extract_dir) / roots[0]

        if IS_FROZEN:
            _update_frozen(extracted_root)
        else:
            _update_source(extracted_root)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _update_source(extracted_root: Path) -> None:
    """Replace spine_swiss_knife/ package files from source ZIP."""
    source_pkg = extracted_root / "spine_swiss_knife"
    if not source_pkg.is_dir():
        raise RuntimeError("spine_swiss_knife/ not found in archive")

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


def _resolve_macos_app_bundle() -> Path:
    """Return the real .app bundle path, resolving AppTranslocation.

    When macOS translocates a downloaded app the running binary sits
    under a read-only ``/var/folders/.../AppTranslocation/...`` path.
    We detect this and fall back to the original location reported by
    the ``xattr`` metadata or ``/Applications`` copy.
    """
    app_bundle = Path(sys.executable).parent.parent.parent

    # Check if running from AppTranslocation (read-only sandbox)
    if "/AppTranslocation/" in str(app_bundle):
        # Try resolving via the bookmark/original path
        try:
            resolved = app_bundle.resolve()
            if "/AppTranslocation/" not in str(resolved):
                return resolved
        except OSError:
            pass

        # Heuristic: look for the same .app name in common locations
        app_name = app_bundle.name  # e.g. "SpineSwissKnife.app"
        for candidate_dir in [
            Path.home() / "Downloads",
            Path("/Applications"),
            Path.home() / "Desktop",
        ]:
            candidate = candidate_dir / app_name
            if candidate.is_dir() and (candidate / "Contents" / "MacOS").is_dir():
                return candidate

        raise RuntimeError(
            f"App is running from a read-only macOS AppTranslocation path.\n\n"
            f"Please move SpineSwissKnife.app to /Applications or your Desktop "
            f"and relaunch, then try updating again."
        )

    return app_bundle


def _update_frozen(extracted_root: Path) -> None:
    """Replace frozen app contents from platform-specific release ZIP.

    macOS: extracted_root contains SpineSwissKnife.app/
    Windows: extracted_root contains SpineSwissKnife/ (with .exe)
    """
    if platform.system() == "Darwin":
        app_bundle = _resolve_macos_app_bundle()
        new_app = extracted_root / "SpineSwissKnife.app"
        if not new_app.is_dir():
            # ZIP root IS the .app bundle (no wrapper folder)
            if (extracted_root / "Contents").is_dir():
                new_app = extracted_root
            else:
                raise RuntimeError("SpineSwissKnife.app not found in archive")

        # Replace Contents/ (skip MacOS/SpineSwissKnife binary — it's running)
        new_contents = new_app / "Contents"
        old_contents = app_bundle / "Contents"
        for item in new_contents.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(new_contents)
            # Skip the running binary itself
            if str(rel) == os.path.join("MacOS", "SpineSwissKnife"):
                continue
            dest = old_contents / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))

    else:
        # Windows: sys.executable = ...\SpineSwissKnife\SpineSwissKnife.exe
        app_dir = Path(sys.executable).parent
        new_dir = extracted_root / "SpineSwissKnife"
        if not new_dir.is_dir():
            # ZIP root IS the app folder (no wrapper folder)
            if (extracted_root / "SpineSwissKnife.exe").is_file():
                new_dir = extracted_root
            else:
                raise RuntimeError("SpineSwissKnife/ not found in archive")

        # Write a batch script that replaces files after we exit
        bat_path = Path(tempfile.gettempdir()) / "ssk_update.bat"
        bat_path.write_text(
            f'@echo off\n'
            f'timeout /t 2 /nobreak >nul\n'
            f'xcopy /e /y /q "{new_dir}\\*" "{app_dir}\\"\n'
            f'start "" "{app_dir}\\SpineSwissKnife.exe"\n'
            f'del "%~f0"\n',
            encoding="utf-8",
        )
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )


def restart_app() -> None:
    """Restart the application."""
    if IS_FROZEN and platform.system() == "Windows":
        # Windows: batch script already handles restart
        sys.exit(0)

    if IS_FROZEN:
        # macOS: relaunch the app bundle (use resolved path)
        app_bundle = _resolve_macos_app_bundle()
        subprocess.Popen(["open", "-n", str(app_bundle)])
    else:
        subprocess.Popen([sys.executable] + sys.argv)

    sys.exit(0)
