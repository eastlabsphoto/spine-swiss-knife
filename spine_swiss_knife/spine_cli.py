"""Spine CLI detection, validation, and export/import wrapper.

Legacy Spine versions (3.x) on macOS Apple Silicon run through the x86
Rosetta launcher.  The built-in preset names (``json``, ``json+pack``, …)
crash with "Error running x86 launcher" on that path.  Using a temporary
export-settings JSON file instead works reliably across all versions.

``--version`` also returns exit-code 1 for legacy versions, so validation
uses ``--help`` (always exit 0) instead.
"""

import json as _json
import platform
import queue
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass
class SpineCliResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    output_json: str = ""
    output_atlas: str = ""


def detect_spine_executable() -> Optional[str]:
    """Look for Spine executable on standard OS paths."""
    system = platform.system()
    candidates: list[str] = []

    if system == "Darwin":
        candidates = [
            "/Applications/Spine.app/Contents/MacOS/Spine",
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Spine\Spine.com",
            r"C:\Program Files (x86)\Spine\Spine.com",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/local/bin/Spine",
            str(Path.home() / "Spine" / "Spine"),
        ]

    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def validate_spine_executable(path: str) -> tuple[bool, str]:
    """Check that *path* points to a working Spine launcher.

    Uses ``--help`` because ``--version`` returns exit-code 1 for legacy
    Spine versions (3.x) running via Rosetta on macOS Apple Silicon.
    ``--help`` is handled by the native launcher itself and always succeeds.
    """
    if not path or not Path(path).is_file():
        return False, "File not found"
    try:
        result = subprocess.run(
            [path, "--help"],
            capture_output=True, text=True, timeout=15,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()

        if "please reinstall" in combined.lower() or "files have been modified" in combined.lower():
            return False, combined

        # --help prints usage info and exits 0 for all launcher versions
        if result.returncode == 0 and "spine" in combined.lower():
            return True, combined

        return False, combined or "Unexpected output from Spine"
    except Exception as e:
        return False, str(e)


def read_spine_file_version(path: str) -> str:
    """Read the Spine editor version from a ``.spine`` project file.

    ``.spine`` files are raw-deflate compressed.  After decompression the
    version string (e.g. ``3.7.94``) appears near the start of the data
    encoded as ASCII with the high bit set on the last character.

    Returns the version string or ``""`` on failure.
    """
    try:
        with open(path, "rb") as f:
            compressed = f.read()
        data = zlib.decompress(compressed, -15)
    except Exception:
        return ""

    # Version string starts at the first ASCII digit in the header.
    start = -1
    for i, b in enumerate(data[:64]):
        if 0x30 <= b <= 0x39:  # '0'-'9'
            start = i
            break
    if start < 0:
        return ""

    chars: list[str] = []
    for i in range(start, min(start + 20, len(data))):
        b = data[i]
        if b & 0x80:
            chars.append(chr(b & 0x7F))
            break
        chars.append(chr(b))
    return "".join(chars)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_cli(
    cmd: list[str],
    timeout: int = 600,
    on_output: Optional[Callable[[str], None]] = None,
) -> tuple[int, str, str]:
    """Run a CLI command, streaming output lines to *on_output* callback.

    Both stdout and stderr are read in background threads.  The main thread
    polls a shared queue with short timeouts so that the caller's event loop
    (e.g. ``QApplication.processEvents()``) stays responsive even when the
    subprocess is silent for several seconds (typical for Rosetta bootstrap).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    q: queue.Queue[Optional[str]] = queue.Queue()

    def _reader(stream, collector):
        for line in stream:
            collector.append(line)
            q.put(line.rstrip())
        q.put(None)  # sentinel — this stream is done

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_lines), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_lines), daemon=True)
    t_out.start()
    t_err.start()

    # Poll the queue, forwarding lines and keeping the caller responsive.
    streams_open = 2
    deadline = time.monotonic() + timeout
    child_pids: list[int] = []
    try:
        while streams_open > 0:
            if time.monotonic() > deadline:
                proc.kill()
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                return -1, "".join(stdout_lines), f"Timed out after {timeout}s"
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                # No output yet — call on_output with None so the caller can
                # pump its event loop (processEvents).
                if on_output:
                    on_output(None)
                continue
            if item is None:
                streams_open -= 1
                continue
            if on_output:
                on_output(item)

        # Collect child PIDs while parent is still alive — on macOS children
        # survive parent exit and get reparented to PID 1 (launchd).
        child_pids = _collect_child_pids(proc.pid)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # cleaned up in finally
        t_out.join(timeout=2)
        t_err.join(timeout=2)
    finally:
        # Kill the parent if still alive
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        # Always kill orphaned children (Spine_x86, java) — they outlive the
        # parent on macOS and can lock .spine files, blocking subsequent ops.
        _kill_pids(child_pids)

    rc = proc.returncode if proc.returncode is not None else -1
    return rc, "".join(stdout_lines), "".join(stderr_lines)


def _collect_child_pids(parent_pid: int) -> list[int]:
    """Collect PIDs of all descendant processes (recursive)."""
    try:
        import psutil
        parent = psutil.Process(parent_pid)
        return [c.pid for c in parent.children(recursive=True)]
    except (ImportError, Exception):
        pass

    # Fallback: pgrep -P, then recurse one level
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split():
            try:
                child_pid = int(line)
                pids.append(child_pid)
                # One more level for Spine -> Spine_x86 -> java
                inner = subprocess.run(
                    ["pgrep", "-P", str(child_pid)],
                    capture_output=True, text=True, timeout=5,
                )
                for inner_line in inner.stdout.strip().split():
                    try:
                        pids.append(int(inner_line))
                    except ValueError:
                        pass
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def _kill_pids(pids: list[int]):
    """Send SIGTERM then SIGKILL to a list of PIDs."""
    import os
    import signal as _signal

    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass

    if not pids:
        return

    # Give them a moment to die, then force-kill survivors.
    # SIGKILL doesn't exist on Windows — SIGTERM already calls
    # TerminateProcess there, so the second pass is skipped.
    sigkill = getattr(_signal, "SIGKILL", None)
    if sigkill is None:
        return
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass


def _write_export_settings(output_dir: str, *, binary: bool = False,
                           pack: bool = False) -> str:
    """Create a temporary export-settings JSON file and return its path.

    Using an explicit settings file avoids the preset names (``json``,
    ``binary``, ``json+pack``, …) which crash on legacy Spine versions
    running through the macOS Rosetta x86 launcher.
    """
    settings: dict = {
        "class": "export-binary" if binary else "export-json",
        "output": str(Path(output_dir).resolve()),
        "open": False,
    }
    if not binary:
        settings["nonessential"] = True
        settings["prettyPrint"] = False
    if pack:
        settings["packAtlas"] = {
            "rotation": False,
            "ignoreBlankImages": True,
            "premultiplyAlpha": False,
            "bleed": True,
            "flattenPaths": True,
        }
        settings["packSource"] = "attachments"
        settings["packTarget"] = "perskeleton"

    fd, path = tempfile.mkstemp(suffix=".json", prefix="ssk_export_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            _json.dump(settings, f)
    except Exception:
        import os
        os.close(fd)
        raise
    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_spine_project(
    exe: str, spine_file: str, output_dir: str,
    timeout: int = 600,
    on_output: Optional[Callable[[str], None]] = None,
    *,
    binary: bool = False,
    pack: bool = True,
) -> SpineCliResult:
    """Export .spine project to JSON (or binary) + optional atlas via CLI.

    Instead of passing a built-in preset name (which breaks on legacy Spine
    versions with the Rosetta launcher), a temporary export-settings JSON
    file is created and passed via ``-e``.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    settings_path = _write_export_settings(output_dir, binary=binary, pack=pack)
    try:
        cmd = [exe]
        version = read_spine_file_version(spine_file)
        if version:
            cmd += ["-u", version]
        cmd += ["-i", spine_file, "-e", settings_path]

        if on_output:
            on_output(f"Running: {' '.join(cmd)}")

        try:
            rc, stdout, stderr = _run_cli(cmd, timeout=timeout, on_output=on_output)
        except Exception as e:
            return SpineCliResult(False, stderr=f"{e}\nCommand: {' '.join(cmd)}")

        combined_info = f"\n\nexit code: {rc}\ncmd: {' '.join(cmd)}"

        if rc != 0:
            return SpineCliResult(False, stdout=stdout, stderr=stderr + combined_info)
    finally:
        # Clean up the temporary settings file
        try:
            Path(settings_path).unlink(missing_ok=True)
        except OSError:
            pass

    # Find exported files
    json_path = ""
    skel_path = ""
    atlas_path = ""
    if out.is_dir():
        for f in out.iterdir():
            if f.suffix == ".json":
                json_path = str(f)
            elif f.suffix == ".skel":
                skel_path = str(f)
            elif f.name.endswith(".atlas") or f.name.endswith(".atlas.txt"):
                atlas_path = str(f)

    data_path = skel_path if binary else json_path

    if not data_path:
        expected = ".skel" if binary else ".json"
        return SpineCliResult(
            False, stdout=stdout,
            stderr=(f"Export finished (exit 0) but no {expected} file found "
                    f"in:\n{out}\n\nstdout: {stdout}\nstderr: {stderr}"),
        )

    if on_output:
        on_output(f"Data: {data_path}")
        if atlas_path:
            on_output(f"Atlas: {atlas_path}")

    return SpineCliResult(
        success=True, stdout=stdout, stderr=stderr,
        output_json=data_path, output_atlas=atlas_path,
    )


def export_first_frames(
    exe: str,
    spine_file: str,
    output_dir: str,
    export_settings: dict,
    animations: list[str],
    timeout: int = 600,
    on_output: Optional[Callable[[str], None]] = None,
) -> dict[str, str]:
    """Export animation frames as PNG via Spine CLI.

    *export_settings* is a dict with Spine CLI ``export-png`` fields.
    The ``output`` and ``open`` keys are overridden automatically.

    Returns a dict mapping animation name -> output PNG path.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    settings = dict(export_settings)
    settings["output"] = str(out.resolve())
    settings["open"] = False

    # Write temp settings with overridden output
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="ssk_img_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            _json.dump(settings, f)
    except Exception:
        import os
        os.close(fd)
        raise

    try:
        cmd = [exe]
        version = read_spine_file_version(spine_file)
        if version:
            cmd += ["-u", version]
        cmd += ["-i", spine_file, "-e", tmp_path]

        if on_output:
            on_output(f"Running: {' '.join(cmd)}")

        try:
            rc, stdout, stderr = _run_cli(cmd, timeout=timeout, on_output=on_output)
        except Exception as e:
            if on_output:
                on_output(f"ERROR: {e}")
            return {}

        if rc != 0:
            if on_output:
                on_output(f"Export failed (exit {rc}): {stderr}")
            return {}
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass

    # Scan output dir for PNGs and match to animation names.
    # Spine typically outputs: <anim_name>/<frame>.png  or  <anim_name>.png
    # For fps=1, first frame is usually 0000.png or the only file.
    results: dict[str, str] = {}
    anim_set = set(animations)

    for png in sorted(out.rglob("*.png")):
        # Try matching by parent folder name (Spine puts each anim in subdir)
        folder_name = png.parent.name
        if folder_name in anim_set and folder_name not in results:
            results[folder_name] = str(png)
            continue
        # Try matching by file stem
        stem = png.stem
        # Strip trailing frame numbers (e.g. "walk0000" -> "walk")
        clean = stem.rstrip("0123456789") or stem
        if clean in anim_set and clean not in results:
            results[clean] = str(png)

    # For animations not yet matched, try looser matching
    for anim in animations:
        if anim in results:
            continue
        for png in sorted(out.rglob("*.png")):
            if anim.lower() in png.stem.lower() or anim.lower() in png.parent.name.lower():
                results[anim] = str(png)
                break

    # Delete everything that wasn't matched to a selected animation
    matched_paths = set(Path(p) for p in results.values())
    for png in list(out.rglob("*.png")):
        if png not in matched_paths:
            try:
                png.unlink()
            except OSError:
                pass
    # Clean up empty subdirectories
    for d in sorted(out.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()  # only removes if empty
            except OSError:
                pass

    if on_output:
        on_output(f"Found {len(results)}/{len(animations)} animation frame(s)")

    return results


def import_to_spine_project(
    exe: str, json_path: str, spine_file: str,
    timeout: int = 600,
    on_output: Optional[Callable[[str], None]] = None,
) -> SpineCliResult:
    """Import modified JSON back into .spine project."""
    cmd = [exe]
    # Determine version: prefer existing .spine, fall back to JSON input
    version = read_spine_file_version(spine_file)
    if not version:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                version = _json.load(f).get("skeleton", {}).get("spine", "")
        except Exception:
            pass
    if version:
        cmd += ["-u", version]
    cmd += ["-i", json_path, "-o", spine_file, "-r"]

    if on_output:
        on_output(f"Running: {' '.join(cmd)}")

    try:
        rc, stdout, stderr = _run_cli(cmd, timeout=timeout, on_output=on_output)
    except Exception as e:
        return SpineCliResult(False, stderr=f"{e}\nCommand: {' '.join(cmd)}")

    if rc != 0:
        return SpineCliResult(
            False, stdout=stdout,
            stderr=stderr + f"\n\nexit code: {rc}\ncmd: {' '.join(cmd)}",
        )

    return SpineCliResult(success=True, stdout=stdout, stderr=stderr)
