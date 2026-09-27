"""macOS conveniences: keeping the Mac awake while working, and mounting the NAS share."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

# Not `sys.platform == "darwin"` inline: type checkers treat that as a constant and flag the
# macOS-only code as unreachable when run on Linux (e.g. in CI).
IS_MACOS = platform.system() == "Darwin"


@contextmanager
def keep_awake() -> Iterator[None]:
    """Stop the Mac idle-sleeping while we work (``caffeinate -i -w <our pid>``).

    ``-w`` makes caffeinate exit by itself if we die, so the Mac can never be kept awake by
    accident. Does nothing on other platforms.
    """
    caffeinate = shutil.which("caffeinate") if IS_MACOS else None
    if caffeinate is None:
        yield
        return
    proc = subprocess.Popen([caffeinate, "-i", "-w", str(os.getpid())])
    try:
        yield
    finally:
        proc.terminate()
        proc.wait()


def ensure_mounted(
    folder: Path,
    mount_url: str,
    *,
    timeout: float = 60.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Make sure ``folder`` is reachable, mounting ``mount_url`` first if it isn't.

    ``mount volume`` reuses the credentials saved in the login Keychain, so it works
    unattended once you've connected once in Finder with "Remember this password" ticked.
    """
    if is_available(folder):
        return
    if not mount_url or not IS_MACOS:
        raise FileNotFoundError(
            f"Recordings folder {folder} not found. Is the NAS mounted? "
            "(Set mount_url in the config to have it mounted automatically.)"
        )
    log.info("%s isn't available; mounting %s", folder, mount_url)
    script = f'mount volume "{_applescript_string(mount_url)}"'
    try:
        result = run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FileNotFoundError(
            f"Mounting {mount_url} timed out. It's probably waiting at a login dialog: connect "
            'once in Finder (Go > Connect to Server) and tick "Remember this password".'
        ) from None
    if result.returncode != 0:
        log.warning("Mounting %s failed: %s", mount_url, result.stderr.strip())
    deadline = time.monotonic() + timeout
    while not is_available(folder):
        if time.monotonic() >= deadline:
            raise FileNotFoundError(
                f"Tried to mount {mount_url}, but {folder} still isn't there. If the share is "
                "mounted under another name (e.g. /Volumes/HyperDeck-1), unmount the old one."
            )
        sleep(1.0)


def is_available(folder: Path) -> bool:
    """True if ``folder`` exists and, for paths in /Volumes, really lives on a mounted volume.

    When a share drops, macOS can leave an empty /Volumes/<name> folder on the boot disk and
    remount the share as /Volumes/<name>-1. Scanning the leftover folder would silently find
    nothing, so treat it as "not mounted".
    """
    if not folder.is_dir():
        return False
    volumes = Path("/Volumes")
    if folder.parts[:2] != volumes.parts:
        return True
    # A real mount has its own device ID; a leftover folder shares /Volumes' device. (Compare
    # with /Volumes rather than /, which is a different, read-only system volume on APFS.)
    return folder.stat().st_dev != volumes.stat().st_dev


def _applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
