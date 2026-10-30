"""Look after the slskd background helper so the user does not have to.

What this does, in order of preference:

1. Notice that slskd is already running and simply use it.
2. Notice that slskd is installed somewhere sensible and start it.
3. Offer to fetch the official slskd release and set it up in Trackfetch's own
   folder, then start that.

Everything it writes lives inside Trackfetch's own data folder. It never
touches, moves, reconfigures or uninstalls the ordinary Soulseek desktop
program, and it never touches an existing slskd install the user set up
themselves; if one is already running it is used as-is.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx

from trackfetch.config.paths import app_paths

log = logging.getLogger(__name__)

GITHUB_LATEST = "https://api.github.com/repos/slskd/slskd/releases/latest"
RELEASE_PAGE = "https://github.com/slskd/slskd/releases/latest"
DEFAULT_PORT = 5030

#: slskd's own defaults are 10s connect and 15s inactivity, but the logs from a
#: real home connection are full of "the wait timed out after 5000 milliseconds".
#: These are deliberately generous. Being patient costs nothing; giving up early
#: costs a reconnection.
CONNECT_TIMEOUT_MS = 30000
INACTIVITY_TIMEOUT_MS = 60000

#: Process names of the ordinary Soulseek desktop client, used only to warn the
#: user about the single-login rule. Trackfetch never stops or alters them.
SOULSEEK_DESKTOP_PROCESSES = (
    "SoulseekQt",
    "SoulseekQt.exe",
    "soulseekqt",
    "Soulseek.exe",
    "slsk.exe",
    "Nicotine",
    "nicotine",
)

ProgressCallback = Callable[[str, float], None]


@dataclass(slots=True)
class SlskdInstallation:
    """Where slskd is and how Trackfetch found it."""

    executable: Path | None
    source: str  # "managed", "path", "installed", "none"
    version: str = ""

    @property
    def found(self) -> bool:
        return self.executable is not None


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _executable_name() -> str:
    return "slskd.exe" if _is_windows() else "slskd"


def _release_asset_suffix() -> str:
    """The asset name fragment for this machine, matching slskd's releases."""
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"} or machine.startswith("armv8")
    if _is_windows():
        return "win-arm64" if arm else "win-x64"
    if sys.platform == "darwin":
        return "osx-arm64" if arm else "osx-x64"
    return "linux-arm64" if arm else "linux-x64"


class SlskdSupervisor:
    """Detect, configure, start and monitor a local slskd."""

    def __init__(
        self,
        managed_dir: Path | None = None,
        port: int = DEFAULT_PORT,
        http_client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self.managed_dir = managed_dir or app_paths().slskd_dir
        self.port = port
        self._process: subprocess.Popen[bytes] | None = None
        #: True when start() found slskd already answering and used it as-is,
        #: rather than launching one. Trackfetch must not stop that one.
        self.adopted_existing = False
        self._client_factory = http_client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))
        )

    # -- detection ---------------------------------------------------------

    @property
    def managed_app_dir(self) -> Path:
        """The app directory Trackfetch hands to slskd. Ours alone."""
        return self.managed_dir / "appdir"

    @property
    def managed_executable(self) -> Path:
        return self.managed_dir / "bin" / _executable_name()

    def candidate_locations(self) -> list[Path]:
        """Where slskd might reasonably live on this machine."""
        names = [_executable_name()]
        found: list[Path] = [self.managed_executable]

        on_path = shutil.which("slskd")
        if on_path:
            found.append(Path(on_path))

        if _is_windows():
            roots = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")),
                Path.home(),
            ]
            for root in roots:
                for name in names:
                    found.append(root / "slskd" / name)
        else:
            for root in (
                Path("/usr/local/bin"),
                Path("/usr/bin"),
                Path("/opt/slskd"),
                Path.home() / ".local" / "bin",
                Path("/Applications/slskd"),
            ):
                for name in names:
                    found.append(root / name)
        return found

    def find_installation(self) -> SlskdInstallation:
        managed = self.managed_executable
        if managed.exists():
            return SlskdInstallation(managed, "managed")
        for candidate in self.candidate_locations():
            try:
                if candidate.exists() and candidate.is_file():
                    source = "path" if shutil.which("slskd") == str(candidate) else "installed"
                    return SlskdInstallation(candidate, source)
            except OSError:  # pragma: no cover - permission oddities
                continue
        return SlskdInstallation(None, "none")

    def is_installed(self) -> bool:
        return self.find_installation().found

    # -- liveness ----------------------------------------------------------

    def port_is_open(self, host: str = "127.0.0.1", port: int | None = None) -> bool:
        target = port or self.port
        try:
            with socket.create_connection((host, target), timeout=1.5):
                return True
        except OSError:
            return False

    def is_running(self, base_url: str | None = None) -> bool:
        """Whether something is answering on the slskd address."""
        url = (base_url or f"http://localhost:{self.port}").rstrip("/")
        try:
            with self._client_factory() as client:
                response = client.get(f"{url}/api/v0/session/enabled")
                return response.status_code < 500
        except httpx.HTTPError:
            return False

    def wait_until_ready(
        self,
        base_url: str | None = None,
        timeout: float = 45.0,
        on_progress: ProgressCallback | None = None,
    ) -> bool:
        """Poll until slskd answers, or give up after ``timeout`` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_running(base_url):
                return True
            if self._process is not None and self._process.poll() is not None:
                log.warning("slskd exited while starting (code %s)", self._process.returncode)
                return False
            if on_progress:
                remaining = max(0.0, deadline - time.time())
                on_progress("Waiting for the Soulseek helper to start", remaining)
            time.sleep(1.0)
        return False

    # -- configuration -----------------------------------------------------

    def write_config(
        self,
        *,
        soulseek_username: str,
        soulseek_password: str,
        web_username: str,
        web_password: str,
        downloads_dir: Path,
        port: int | None = None,
        shared_dirs: Iterable[Path] | None = None,
    ) -> Path:
        """Write the slskd.yml that Trackfetch manages.

        Only ever written inside Trackfetch's own folder. An slskd the user
        installed and configured themselves is left completely alone.
        """
        target_port = port or self.port
        app_dir = self.managed_app_dir
        app_dir.mkdir(parents=True, exist_ok=True)
        incomplete = app_dir / "incomplete"
        incomplete.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)

        shares = list(shared_dirs or [])
        share_lines = "\n".join(f"    - {_yaml_str(str(p))}" for p in shares)
        if not share_lines:
            share_lines = "    []"

        content = f"""# Written and managed by Trackfetch. Editing it by hand is not necessary.
soulseek:
  username: {_yaml_str(soulseek_username)}
  password: {_yaml_str(soulseek_password)}
  description: "Trackfetch"
  connection:
    timeout:
      # More patient than the defaults. Home connections and busy Soulseek
      # servers routinely take longer than 5 seconds to answer, and a timeout
      # there is what shows up as a disconnection.
      connect: {int(CONNECT_TIMEOUT_MS)}
      inactivity: {int(INACTIVITY_TIMEOUT_MS)}
directories:
  downloads: {_yaml_str(str(downloads_dir))}
  incomplete: {_yaml_str(str(incomplete))}
shares:
  directories:
{share_lines}
web:
  port: {int(target_port)}
  authentication:
    username: {_yaml_str(web_username)}
    password: {_yaml_str(web_password)}
flags:
  no_version_check: false
"""
        config_path = app_dir / "slskd.yml"
        tmp = config_path.with_suffix(".yml.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(config_path)
        try:
            os.chmod(config_path, 0o600)
        except OSError:  # pragma: no cover - Windows ignores this
            pass
        return config_path

    @property
    def config_path(self) -> Path:
        return self.managed_app_dir / "slskd.yml"

    def has_managed_config(self) -> bool:
        return self.config_path.exists()

    # -- starting and stopping --------------------------------------------

    def start(
        self,
        installation: SlskdInstallation | None = None,
        use_managed_config: bool = True,
    ) -> bool:
        """Start slskd in the background. Returns True if a process was launched."""
        install = installation or self.find_installation()
        if not install.found or install.executable is None:
            return False
        if self.is_running():
            # Something is already answering. It might be an slskd Trackfetch
            # started on a previous run and failed to stop, or one the user runs
            # themselves. Either way we use it rather than starting a second.
            if self._recorded_pid() is None:
                self.adopted_existing = True
                log.info("slskd was already running; using it instead of starting one")
            return True

        command = [str(install.executable)]
        if use_managed_config and self.has_managed_config():
            command += ["--app-dir", str(self.managed_app_dir)]

        creation_flags = 0
        startupinfo = None
        if _is_windows():  # pragma: no cover - Windows only
            creation_flags = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        log_file = app_paths().log_dir / "slskd-stdout.log"
        try:
            handle = open(log_file, "ab", buffering=0)
        except OSError:  # pragma: no cover - defensive
            handle = subprocess.DEVNULL  # type: ignore[assignment]

        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed, local executable
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(install.executable.parent),
                creationflags=creation_flags,
                startupinfo=startupinfo,
                close_fds=True,
            )
        except OSError as exc:
            log.error("Could not start slskd: %s", exc)
            return False
        self.adopted_existing = False
        self._record_pid(self._process.pid, install.executable)
        log.info("Started slskd from %s (pid %s)", install.executable, self._process.pid)
        return True

    # -- remembering the process across runs -------------------------------

    @property
    def pid_file(self) -> Path:
        """Where Trackfetch notes down the helper it started.

        Without this, closing Trackfetch and opening it again loses track of the
        slskd it launched, which then stays running for ever and keeps hold of
        the Soulseek account.
        """
        return self.managed_dir / "helper.pid"

    def _record_pid(self, pid: int, executable: Path) -> None:
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(
                json.dumps({"pid": int(pid), "executable": str(executable), "at": time.time()}),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not note down the helper's process id: %s", exc)

    def _forget_pid(self) -> None:
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    def _recorded_pid(self) -> int | None:
        """The pid Trackfetch last started, if that process is still slskd."""
        try:
            raw = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = raw.get("pid") if isinstance(raw, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return None
        if not _pid_is_slskd(pid):
            # The process is gone, or the number has been handed to something
            # else entirely. Never signal a pid we cannot identify.
            self._forget_pid()
            return None
        return pid

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the helper, but only one Trackfetch started itself.

        Works both for a process started in this run and for one left behind by
        an earlier run, which is the case that was leaving slskd holding the
        Soulseek account after Trackfetch had closed. An slskd that was already
        running when Trackfetch opened is never touched.
        """
        if self.adopted_existing:
            log.info("Leaving slskd running: Trackfetch did not start it")
            return False

        stopped = False
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=timeout)
                stopped = True
            except Exception:  # pragma: no cover - defensive
                try:
                    process.kill()
                    stopped = True
                except Exception:
                    pass
        self._process = None

        if not stopped:
            pid = self._recorded_pid()
            if pid is not None:
                stopped = _terminate_pid(pid, timeout=timeout)
                if stopped:
                    log.info("Stopped a Soulseek helper left over from an earlier run (pid %s)", pid)

        self._forget_pid()
        if stopped:
            log.info("Soulseek helper stopped")
        return stopped

    @property
    def started_by_trackfetch(self) -> bool:
        if self.adopted_existing:
            return False
        if self._process is not None and self._process.poll() is None:
            return True
        return self._recorded_pid() is not None

    def stoppable(self) -> bool:
        """Whether there is a helper Trackfetch is allowed to stop."""
        return self.started_by_trackfetch

    # -- installing --------------------------------------------------------

    def latest_release(self) -> dict[str, object]:
        with self._client_factory() as client:
            response = client.get(
                GITHUB_LATEST, headers={"Accept": "application/vnd.github+json"}
            )
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, dict) else {}

    def find_release_asset(self, release: dict[str, object]) -> tuple[str, str]:
        """Return (asset name, download URL) for this machine."""
        suffix = _release_asset_suffix()
        assets = release.get("assets")
        if not isinstance(assets, list):
            return "", ""
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name", ""))
            if suffix in name and name.endswith(".zip") and "musl" not in name:
                return name, str(asset.get("browser_download_url", ""))
        return "", ""

    def install_latest(self, on_progress: ProgressCallback | None = None) -> Path | None:
        """Download the official slskd release into Trackfetch's own folder.

        Returns the path to the executable, or None if it could not be done.
        Nothing outside Trackfetch's data folder is written or changed.
        """

        def report(message: str, fraction: float) -> None:
            if on_progress:
                on_progress(message, fraction)

        report("Looking up the latest Soulseek helper", 0.05)
        try:
            release = self.latest_release()
        except Exception as exc:
            log.error("Could not look up the slskd release: %s", exc)
            return None

        name, url = self.find_release_asset(release)
        if not url:
            log.error("No slskd download available for this computer (%s)", _release_asset_suffix())
            return None

        report(f"Downloading {name}", 0.15)
        buffer = io.BytesIO()
        try:
            with self._client_factory() as client:
                with client.stream("GET", url, follow_redirects=True) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length") or 0)
                    seen = 0
                    for chunk in response.iter_bytes(chunk_size=131072):
                        buffer.write(chunk)
                        seen += len(chunk)
                        if total:
                            report(f"Downloading {name}", 0.15 + 0.6 * seen / total)
        except Exception as exc:
            log.error("Downloading slskd failed: %s", exc)
            return None

        report("Unpacking the Soulseek helper", 0.8)
        target = self.managed_dir / "bin"
        try:
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            buffer.seek(0)
            with zipfile.ZipFile(buffer) as archive:
                _safe_extract(archive, target)
        except Exception as exc:
            log.error("Unpacking slskd failed: %s", exc)
            return None

        executable = target / _executable_name()
        if not executable.exists():
            # Some archives nest everything one level down.
            for path in target.rglob(_executable_name()):
                executable = path
                break
        if not executable.exists():
            log.error("The slskd download did not contain an executable")
            return None
        if not _is_windows():
            try:
                executable.chmod(executable.stat().st_mode | 0o111)
            except OSError:  # pragma: no cover - defensive
                pass

        report("Soulseek helper ready", 1.0)
        return executable

    # -- the other Soulseek program ---------------------------------------

    def desktop_soulseek_running(self) -> bool:
        """Is the ordinary Soulseek program open right now?

        Soulseek allows one login per account at a time, so running the normal
        desktop client and Trackfetch on the same account will knock one of them
        off. Trackfetch only looks; it never closes or changes anything.
        """
        try:
            if _is_windows():  # pragma: no cover - Windows only
                output = subprocess.run(  # noqa: S603
                    ["tasklist"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=0x08000000,
                ).stdout
            else:
                output = subprocess.run(  # noqa: S603
                    ["ps", "-A", "-o", "comm="],
                    capture_output=True,
                    text=True,
                    timeout=8,
                ).stdout
        except Exception:
            return False
        lowered = output.lower()
        return any(name.lower() in lowered for name in SOULSEEK_DESKTOP_PROCESSES)

    # -- diagnostics -------------------------------------------------------

    def describe(self) -> dict[str, object]:
        install = self.find_installation()
        return {
            "installed": install.found,
            "install_source": install.source,
            "executable": str(install.executable) if install.executable else "",
            "managed_app_dir": str(self.managed_app_dir),
            "managed_config_present": self.has_managed_config(),
            "port": self.port,
            "running": self.is_running(),
            "started_by_trackfetch": self.started_by_trackfetch,
            "adopted_existing": self.adopted_existing,
        }


def _pid_is_slskd(pid: int) -> bool:
    """Whether ``pid`` is alive and really is slskd.

    Process ids get reused, so Trackfetch checks the name before signalling
    anything. If the answer cannot be established, the answer is no.
    """
    try:
        if _is_windows():  # pragma: no cover - Windows only
            output = subprocess.run(  # noqa: S603
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=0x08000000,
            ).stdout
        else:
            output = subprocess.run(  # noqa: S603
                ["ps", "-p", str(int(pid)), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=8,
            ).stdout
    except Exception:
        return False
    return "slskd" in output.lower()


def _terminate_pid(pid: int, timeout: float = 10.0) -> bool:
    """Ask a process to close, then insist. Returns True if it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        log.warning("Could not stop the Soulseek helper (pid %s): %s", pid, exc)
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_slskd(pid):
            return True
        time.sleep(0.3)

    try:  # pragma: no cover - only when SIGTERM is ignored
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, ValueError):
        pass
    time.sleep(0.5)
    return not _pid_is_slskd(pid)


def _yaml_str(value: str) -> str:
    """Quote a value so it is always a valid YAML double-quoted scalar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    """Extract a zip without letting any entry escape the target folder."""
    root = target.resolve()
    for member in archive.infolist():
        name = member.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            log.warning("Refusing to extract suspicious archive entry: %s", name)
            continue
        destination = (root / name).resolve()
        if not str(destination).startswith(str(root)):
            log.warning("Refusing to extract entry outside the target: %s", name)
            continue
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(destination, "wb") as out:
            shutil.copyfileobj(source, out)
