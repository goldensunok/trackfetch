"""SFTP servers.

Workflow: connect, browse, choose, download. Trackfetch fetches the bytes itself
here, using Paramiko's SFTP client.

Saved servers live in the settings file; their passwords live in the operating
system credential store, never in the settings file and never in the log.
Downloads resume from a part-finished file where the server allows it.

Browsing uses a simple two-part path: ``server name:/remote/path``. Asking for
an empty path lists the saved servers.
"""

from __future__ import annotations

import logging
import stat
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from trackfetch.models.common import (
    Capability,
    ConnectionState,
    ConnectionStatus,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
    coerce_int,
    coerce_str,
)
from trackfetch.providers.base import BrowseEntry, ProgressCallback, Provider, ProviderContext
from trackfetch.providers.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    DestinationError,
    ProviderNotConfigured,
    RemoteServiceError,
    TrackfetchError,
)
from trackfetch.services.destinations import ensure_parent, sanitise_filename

log = logging.getLogger(__name__)

PROVIDER_ID = "sftp"
SERVICE = "SFTP"
CHUNK = 262144

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name="SFTP",
    capabilities=frozenset(
        {
            Capability.BROWSE,
            Capability.DOWNLOAD,
            Capability.PROGRESS,
            Capability.RESUME,
            Capability.CANCEL,
            Capability.AUTH,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Connect to a server, browse it, choose files and download them.",
)


class SftpProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._cancelled: set[str] = set()
        self._guard = threading.RLock()

    # -- configuration -----------------------------------------------------

    def servers(self) -> list[dict[str, Any]]:
        value = self.ctx.config("servers", [])
        return [s for s in value if isinstance(s, dict)] if isinstance(value, list) else []

    def server(self, name: str = "") -> dict[str, Any]:
        servers = self.servers()
        if not servers:
            raise ProviderNotConfigured(
                "No SFTP server has been set up yet.",
                "Open Settings, choose SFTP and add a server.",
                provider_id=PROVIDER_ID,
            )
        if not name:
            return servers[0]
        for entry in servers:
            if coerce_str(entry.get("name")) == name:
                return entry
        raise ProviderNotConfigured(
            f"There is no saved server called {name}.",
            "Open Settings, choose SFTP and check the saved servers.",
            provider_id=PROVIDER_ID,
        )

    def is_configured(self) -> bool:
        return bool(self.servers())

    def _password(self, server: dict[str, Any]) -> str:
        return self.ctx.secret(f"{coerce_str(server.get('name'))}.password")

    # -- connecting --------------------------------------------------------

    def connect(self, server: dict[str, Any]):
        """Open an SFTP connection. The caller closes it."""
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - packaging problem
            raise RemoteServiceError(
                "SFTP support is missing from this copy of Trackfetch.",
                "Use Help then Copy Diagnostic Information and send the result on.",
                technical=repr(exc),
                provider_id=PROVIDER_ID,
            ) from exc

        host = coerce_str(server.get("host"))
        port = coerce_int(server.get("port")) or 22
        username = coerce_str(server.get("username"))
        key_file = coerce_str(server.get("key_file"))
        password = self._password(server)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Servers a person adds themselves are usually not in the known-hosts
        # file. Accepting a new host key is the practical behaviour here; the
        # connection is still encrypted.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username or None,
                password=password or None,
                key_filename=key_file or None,
                timeout=15,
                banner_timeout=20,
                auth_timeout=20,
                look_for_keys=bool(key_file),
                allow_agent=False,
            )
        except Exception as exc:
            client.close()
            raise _translate(exc, host) from exc
        return client

    def test_connection(self) -> ConnectionStatus:
        if not self.enabled:
            return self._set_status(
                ConnectionStatus(
                    provider_id=PROVIDER_ID,
                    state=ConnectionState.DISABLED,
                    detail="SFTP is switched off.",
                    remedy="Open Settings and switch it on.",
                )
            )
        if not self.is_configured():
            return self._status_not_configured(
                "No SFTP server has been set up yet.",
                "Open Settings, choose SFTP and add a server.",
            )
        results: list[str] = []
        problems: list[TrackfetchError] = []
        for server in self.servers():
            name = coerce_str(server.get("name"))
            client = None
            try:
                client = self.connect(server)
                sftp = client.open_sftp()
                sftp.listdir(coerce_str(server.get("start_path")) or ".")
                sftp.close()
                results.append(name)
            except TrackfetchError as exc:
                problems.append(exc)
            except Exception as exc:  # pragma: no cover - defensive
                problems.append(_translate(exc, name))
            finally:
                if client is not None:
                    client.close()
        if problems and not results:
            return self._status_from_error(problems[0])
        detail = f"{len(results)} server{'s' if len(results) != 1 else ''} reachable."
        if problems:
            detail += f" {len(problems)} could not be reached."
        return self._status_ok(detail)

    # -- browsing ----------------------------------------------------------

    def browse(self, path: str = "") -> list[BrowseEntry]:
        if not path:
            return [
                BrowseEntry(
                    name=coerce_str(s.get("name")),
                    path=f"{coerce_str(s.get('name'))}:{coerce_str(s.get('start_path')) or '.'}",
                    is_dir=True,
                    extra={"server": coerce_str(s.get("name"))},
                )
                for s in self.servers()
            ]

        server_name, remote = _split_path(path)
        server = self.server(server_name)
        client = self.connect(server)
        try:
            sftp = client.open_sftp()
            entries: list[BrowseEntry] = []
            for attr in sftp.listdir_attr(remote or "."):
                is_dir = stat.S_ISDIR(attr.st_mode or 0)
                child = str(PurePosixPath(remote or ".") / attr.filename)
                entries.append(
                    BrowseEntry(
                        name=attr.filename,
                        path=f"{server_name}:{child}",
                        is_dir=is_dir,
                        size=None if is_dir else attr.st_size,
                        modified=float(attr.st_mtime) if attr.st_mtime else None,
                        extra={"server": server_name},
                    )
                )
            sftp.close()
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return entries
        finally:
            client.close()

    def candidate_from_entry(self, entry: BrowseEntry) -> SearchCandidate:
        server_name, remote = _split_path(entry.path)
        name = PurePosixPath(remote).name
        candidate = SearchCandidate(
            provider_id=PROVIDER_ID,
            filename=name,
            directory=str(PurePosixPath(remote).parent),
            extension=Path(name).suffix.lstrip(".").lower(),
            size=entry.size,
            remote_user=server_name,
            relevance=1.0,
        )
        candidate.payload = {"server": server_name, "remote_path": remote}
        return candidate

    # -- downloading -------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.payload.setdefault("server", candidate.remote_user)
        job.payload.setdefault("remote_path", candidate.payload.get("remote_path", ""))
        return job

    def download(
        self,
        job: TransferJob,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> TransferJob:
        remote = coerce_str(job.payload.get("remote_path"))
        if not remote:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch does not know which file to fetch."
            job.remedy = "Browse the server again and choose the file."
            return job

        server = self.server(coerce_str(job.payload.get("server")))
        target = _target_path(job, PurePosixPath(remote).name)
        part = target.with_name(target.name + ".part")

        # Drop any stale cancel flag from an earlier attempt, so a retry is not
        # stopped the instant it starts.
        with self._guard:
            self._cancelled.discard(job.id)

        try:
            ensure_parent(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = DestinationError.default_message
            job.remedy = DestinationError.default_remedy
            log.warning("cannot create %s: %s", target.parent, exc)
            return job

        client = None
        try:
            client = self.connect(server)
            sftp = client.open_sftp()
            info = sftp.stat(remote)
            total = int(info.st_size or 0)
            job.total_bytes = total

            existing = part.stat().st_size if part.exists() else 0
            if existing > total:
                existing = 0
                part.unlink(missing_ok=True)

            job.status = TransferStatus.DOWNLOADING
            started = time.time()
            written = existing
            last_report = 0.0

            with sftp.open(remote, "rb") as source:
                source.prefetch(total)
                if existing:
                    source.seek(existing)
                with open(part, "ab" if existing else "wb") as handle:
                    while True:
                        if cancel is not None and cancel.is_set():
                            job.status = TransferStatus.CANCELLED
                            return job
                        with self._guard:
                            if job.id in self._cancelled:
                                self._cancelled.discard(job.id)
                                job.status = TransferStatus.CANCELLED
                                return job
                        chunk = source.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        job.downloaded_bytes = written
                        elapsed = max(1e-6, time.time() - started)
                        speed = (written - existing) / elapsed
                        job.speed_bps = speed
                        if total and speed > 0:
                            job.eta_seconds = int(max(0, (total - written) / speed))
                        now = time.time()
                        if on_progress and now - last_report > 0.25:
                            last_report = now
                            on_progress(written, total or None, speed)
            sftp.close()
        except TrackfetchError as exc:
            job.status = TransferStatus.FAILED
            job.error = exc.message
            job.remedy = exc.remedy
            return job
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "The connection to the server was interrupted."
            job.remedy = "Click Retry. Trackfetch will carry on from where it stopped."
            log.debug("sftp transfer interrupted: %r", exc)
            return job
        except Exception as exc:  # pragma: no cover - defensive
            job.status = TransferStatus.FAILED
            translated = _translate(exc, coerce_str(server.get("host")))
            job.error = translated.message
            job.remedy = translated.remedy
            return job
        finally:
            if client is not None:
                client.close()

        try:
            if target.exists():
                target.unlink()
            part.replace(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not save the finished file."
            job.remedy = "Check the download folder still exists and is writable."
            log.warning("could not move %s: %s", part, exc)
            return job

        job.status = TransferStatus.COMPLETE
        job.destination = str(target)
        job.downloaded_bytes = job.total_bytes or target.stat().st_size
        job.completed_at = time.time()
        job.speed_bps = None
        job.eta_seconds = None
        job.touch()
        return job

    def cancel(self, job: TransferJob) -> bool:
        with self._guard:
            self._cancelled.add(job.id)
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        return []


def _target_path(job: TransferJob, remote_name: str) -> Path:
    """Where the file should end up.

    The queue decides the exact path and puts it in ``target_path``. It must be
    used as given: guessing from whether it has a file extension would turn a
    file called ``CHANGELOG`` into a folder of the same name.
    """
    chosen = coerce_str(job.payload.get("target_path"))
    if chosen:
        return Path(chosen)
    base = Path(job.destination)
    if base.is_dir() or not base.suffix:
        return base / sanitise_filename(remote_name)
    return base


def _split_path(path: str) -> tuple[str, str]:
    if ":" in path:
        name, _, remote = path.partition(":")
        return name, remote or "."
    return "", path or "."


def _translate(exc: Exception, host: str) -> TrackfetchError:
    """Turn a Paramiko or socket failure into something a person can act on."""
    name = type(exc).__name__
    text = str(exc).lower()
    if "authentication" in name.lower() or "authentication" in text:
        return AuthenticationFailed(
            "The server rejected your SFTP login. Check the username and password.",
            "Open Settings, choose SFTP and check the details for that server.",
            technical=repr(exc),
            provider_id=PROVIDER_ID,
        )
    if "no such file" in text or isinstance(exc, FileNotFoundError):
        return RemoteServiceError(
            "That file is no longer on the server.",
            "Browse the server again and choose another file.",
            technical=repr(exc),
            provider_id=PROVIDER_ID,
        )
    if "permission" in text:
        return RemoteServiceError(
            "The server would not let you read that file.",
            "Check your account has permission to read it.",
            technical=repr(exc),
            provider_id=PROVIDER_ID,
        )
    return ConnectionFailed(
        f"Trackfetch could not reach the SFTP server {host}.".strip(),
        "Check the address and port, and that the server is switched on.",
        technical=repr(exc),
        provider_id=PROVIDER_ID,
    )


def create(ctx: ProviderContext) -> SftpProvider:
    return SftpProvider(ctx)
