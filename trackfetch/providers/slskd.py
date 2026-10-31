"""Soulseek, through slskd.

This is the provider the whole application exists for. It gives the workflow:

    search  ->  results  ->  the user chooses  ->  download

Trackfetch never implements the Soulseek protocol. It drives a locally running
slskd, which is a real, maintained Soulseek client, over slskd's documented HTTP
API. See ``slskd_client.py`` for the endpoint list.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from trackfetch.models.common import (
    Capability,
    ConnectionState,
    ConnectionStatus,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
    coerce_bool,
    coerce_int,
    coerce_str,
)
from trackfetch.providers.base import Provider, ProviderContext, SearchOptions
from trackfetch.providers.errors import (
    SlskdUnavailable,
    SoulseekNotConnected,
    TrackfetchError,
)
from trackfetch.providers.slskd_client import (
    SlskdClient,
    classify_transfer_state,
    parse_search_files,
    split_soulseek_path,
)
from trackfetch.services import relevance

log = logging.getLogger(__name__)

PROVIDER_ID = "slskd"

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name="Soulseek",
    capabilities=frozenset(
        {
            Capability.SEARCH,
            Capability.ADD_JOB,
            Capability.QUEUE,
            Capability.PROGRESS,
            Capability.CANCEL,
            Capability.AUTH,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Search the Soulseek network, choose files, then download them.",
    requires_app="slskd",
)


class SlskdProvider(Provider):
    """Adapter between Trackfetch and a local slskd instance."""

    info = INFO

    def __init__(self, ctx: ProviderContext, supervisor: Any | None = None):
        super().__init__(ctx)
        self._client: SlskdClient | None = None
        self._client_lock = threading.RLock()
        self._supervisor = supervisor
        self._start_attempted = False
        #: Maps our job id to (soulseek username, remote filename).
        self._job_index: dict[str, tuple[str, str]] = {}

    # -- wiring ------------------------------------------------------------

    @property
    def supervisor(self):
        if self._supervisor is None:
            from trackfetch.services.slskd_supervisor import SlskdSupervisor

            port = _port_from_url(self.ctx.config("base_url", "http://localhost:5030"))
            self._supervisor = SlskdSupervisor(port=port)
        return self._supervisor

    def _build_client(self) -> SlskdClient:
        return SlskdClient(
            base_url=self.ctx.config("base_url", "http://localhost:5030"),
            api_key=self.ctx.secret("api_key"),
            username=coerce_str(self.ctx.config("username", "slskd")),
            password=self.ctx.secret("password"),
            url_base=coerce_str(self.ctx.config("url_base", "")),
            verify_tls=bool(self.ctx.config("verify_tls", True)),
            timeout=float(self.ctx.config("timeout", 30) or 30),
        )

    @property
    def client(self) -> SlskdClient:
        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
            return self._client

    def reset_client(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
            self._client = None
            self._start_attempted = False

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def is_configured(self) -> bool:
        if not self.ctx.config("base_url"):
            return False
        if self.ctx.secret("api_key"):
            return True
        return bool(self.ctx.config("username")) and bool(self.ctx.secret("password"))

    # -- connection --------------------------------------------------------

    def ensure_available(self, allow_start: bool = True) -> None:
        """Make sure slskd is up and Trackfetch is signed in to it."""
        self.require_configured()
        client = self.client
        if not client.ping():
            if allow_start and self.ctx.config("manage_slskd", True) and not self._start_attempted:
                self._start_attempted = True
                self._try_start_slskd()
            if not client.ping():
                raise SlskdUnavailable(provider_id=PROVIDER_ID)
        client.authenticate()

    def _try_start_slskd(self) -> bool:
        supervisor = self.supervisor
        install = supervisor.find_installation()
        if not install.found:
            log.info("slskd is not installed, cannot start it automatically")
            return False
        self.ctx.event("Starting the Soulseek helper", "info")
        if not supervisor.start(install):
            return False
        ready = supervisor.wait_until_ready(
            self.ctx.config("base_url", "http://localhost:5030"), timeout=45
        )
        if ready:
            self.ctx.event("The Soulseek helper is running", "info")
        return ready

    def test_connection(self) -> ConnectionStatus:
        """A look, never a nudge. Safe for the automatic refresh to call."""
        return self.check_connection(interactive=False)

    def check_connection(self, interactive: bool = False) -> ConnectionStatus:
        if not self.enabled:
            return self._set_status(
                ConnectionStatus(
                    provider_id=PROVIDER_ID,
                    state=ConnectionState.DISABLED,
                    detail="Soulseek is switched off.",
                    remedy="Open Settings, choose Soulseek and switch it on.",
                )
            )
        if not self.is_configured():
            return self._status_not_configured(
                "Soulseek has not been set up yet.",
                "Open Settings, choose Soulseek and enter your Soulseek username "
                "and password.",
            )
        try:
            self.ensure_available(allow_start=interactive)
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)

        try:
            connected, description = self.client.is_connected_to_soulseek()
        except TrackfetchError as exc:
            return self._status_from_error(exc)

        if not connected and interactive:
            # One nudge, and only because the user just asked for one by pressing
            # a button. Doing this from the automatic refresh would cancel
            # slskd's own patient reconnection schedule and force a fresh login
            # attempt every single time the status was checked.
            self.client.connect_server()
            time.sleep(1.5)
            try:
                connected, description = self.client.is_connected_to_soulseek()
            except TrackfetchError as exc:
                return self._status_from_error(exc)

        if connected:
            return self._status_ok("Connected to Soulseek.")

        remedy = SoulseekNotConnected.default_remedy
        if self._desktop_client_running():
            remedy = (
                "The ordinary Soulseek program appears to be open. Soulseek only "
                "allows one login per account at a time, so please close it and "
                "then click Test Connection again."
            )
        return self._set_status(
            ConnectionStatus(
                provider_id=PROVIDER_ID,
                state=ConnectionState.DISCONNECTED,
                detail="Trackfetch reached the Soulseek helper but is not signed in "
                "to Soulseek.",
                remedy=remedy,
                technical=description,
            )
        )

    def _desktop_client_running(self) -> bool:
        try:
            return bool(self.supervisor.desktop_soulseek_running())
        except Exception:  # pragma: no cover - defensive
            return False

    # -- search ------------------------------------------------------------

    def search(
        self,
        text: str,
        options: SearchOptions | None = None,
        cancel: threading.Event | None = None,
    ) -> list[SearchCandidate]:
        """Run a real Soulseek search and return every file that came back."""
        opts = options or SearchOptions()
        self.ensure_available()

        connected, description = self.client.is_connected_to_soulseek()
        if not connected:
            raise SoulseekNotConnected(technical=description, provider_id=PROVIDER_ID)

        state, responses = self.client.run_search(
            text,
            timeout_seconds=opts.timeout,
            response_limit=opts.response_limit,
            file_limit=opts.file_limit,
            minimum_response_file_count=opts.minimum_response_file_count,
            maximum_peer_queue_length=opts.maximum_peer_queue_length,
            minimum_peer_upload_speed=opts.minimum_peer_upload_speed,
            cancel=cancel,
        )
        records = parse_search_files(responses, include_locked=True)
        return [self._to_candidate(text, record) for record in records]

    def _to_candidate(self, query_text: str, record: dict[str, Any]) -> SearchCandidate:
        peer = record.get("peer") or {}
        full_path = coerce_str(record.get("filename"))
        directory, filename = split_soulseek_path(full_path)
        extension = coerce_str(record.get("extension")).lstrip(".").lower()
        if not extension and "." in filename:
            extension = filename.rsplit(".", 1)[-1].lower()

        candidate = SearchCandidate(
            provider_id=PROVIDER_ID,
            filename=filename,
            directory=directory,
            extension=extension,
            size=coerce_int(record.get("size")),
            bitrate=coerce_int(record.get("bitRate")),
            is_vbr=coerce_bool(record.get("isVariableBitRate")),
            duration=coerce_int(record.get("length")),
            sample_rate=coerce_int(record.get("sampleRate")),
            bit_depth=coerce_int(record.get("bitDepth")),
            codec=extension.upper() if extension else "",
            remote_user=coerce_str(peer.get("username")),
            queue_length=coerce_int(peer.get("queueLength")),
            has_free_slot=coerce_bool(peer.get("hasFreeUploadSlot")),
            upload_speed=coerce_int(peer.get("uploadSpeed")),
            locked=bool(record.get("locked")),
            raw=dict(record),
        )
        candidate.relevance = relevance.score(query_text, filename, directory)
        candidate.payload = {
            "username": candidate.remote_user,
            "remote_filename": full_path,
            "size": candidate.size or 0,
        }
        return candidate

    # -- transfers ---------------------------------------------------------

    def build_job(
        self,
        candidate: SearchCandidate,
        destination_dir: Path,
        label: str = "",
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.payload.setdefault("username", candidate.remote_user)
        job.payload.setdefault("remote_filename", candidate.payload.get("remote_filename", ""))
        job.payload.setdefault("size", candidate.size or 0)
        job.label = label or candidate.filename or candidate.display_path
        return job

    def submit(self, job: TransferJob) -> TransferJob:
        """Tell slskd to fetch this specific file from this specific user."""
        username = coerce_str(job.payload.get("username"))
        remote_filename = coerce_str(job.payload.get("remote_filename"))
        if not username or not remote_filename:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch does not have enough detail to fetch that file."
            job.remedy = "Search again and choose the file once more."
            job.touch()
            return job

        self.ensure_available()
        size = coerce_int(job.payload.get("size")) or 0

        try:
            self.client.enqueue(
                username,
                [{"filename": remote_filename, "size": size}],
                destination=self._relative_destination(job),
            )
        except TrackfetchError as exc:
            job.status = TransferStatus.FAILED
            job.error = exc.message
            job.remedy = exc.remedy
            job.touch()
            self.ctx.event(f"Could not queue {job.label}: {exc.message}", "error")
            return job

        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.remote_id = f"{username}|{remote_filename}"
        # Remember when we asked, so a transfer slskd later forgets about does
        # not leave the job sitting in Queued for ever with no way out.
        job.payload["submitted_at"] = time.time()
        job.payload.pop("transfer_id", None)
        self._job_index[job.id] = (username, remote_filename)
        job.touch()
        self.ctx.event(f"Asked Soulseek for {job.label}", "info")
        return job

    def _relative_destination(self, job: TransferJob) -> str | None:
        """A subfolder for slskd, expressed the only way slskd will accept it.

        slskd refuses an absolute path outright: its destination must be
        relative to the download folder it was configured with, and must not
        climb out of it. Sending a full Windows path meant every download came
        back as "the Soulseek helper refused the request" with nothing to show
        why. Where the job's folder sits inside slskd's own, the difference
        between them is sent; where it does not, nothing is sent and the file
        lands in slskd's folder, which is the one Trackfetch set up.
        """
        wanted = str(job.destination or "").strip()
        if not wanted:
            return None
        try:
            root = Path(str(self.ctx.download_dir())).expanduser().resolve()
            target = Path(wanted).expanduser().resolve()
        except (OSError, ValueError):  # pragma: no cover - odd paths
            return None
        if target == root:
            return None
        try:
            relative = target.relative_to(root)
        except ValueError:
            # Somewhere else entirely. slskd cannot write there on our behalf,
            # so let the file land in its own folder rather than failing.
            return None
        text = relative.as_posix().strip("/")
        return text or None

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        """Refresh Soulseek transfers from slskd."""
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        if not relevant:
            return []
        try:
            self.ensure_available(allow_start=False)
            transfers = self.client.downloads()
        except TrackfetchError as exc:
            log.debug("Could not read Soulseek transfers: %s", exc.message)
            return []
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Could not read Soulseek transfers: %r", exc)
            return []

        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[tuple[str, str], dict[str, Any]] = {}
        for record in transfers:
            transfer_id = coerce_str(record.get("id"))
            if transfer_id:
                by_id[transfer_id] = record
            key = (
                coerce_str(record.get("username")),
                coerce_str(record.get("filename")),
            )
            by_name.setdefault(key, record)

        changed: list[TransferJob] = []
        claimed: set[str] = set()

        # Jobs that already know their slskd transfer id are matched on it
        # first. Matching on the file name alone would let a stale record from
        # an earlier attempt, or a second copy of the same file, be applied to
        # the wrong job.
        for job in relevant:
            transfer_id = coerce_str(job.payload.get("transfer_id"))
            record = by_id.get(transfer_id) if transfer_id else None
            if record is None:
                username = coerce_str(job.payload.get("username"))
                filename = coerce_str(job.payload.get("remote_filename"))
                candidate = by_name.get((username, filename))
                if candidate is not None:
                    candidate_id = coerce_str(candidate.get("id"))
                    if candidate_id and candidate_id in claimed:
                        candidate = None
                record = candidate
            if record is None:
                if self._give_up_on_missing(job):
                    changed.append(job)
                continue
            record_id = coerce_str(record.get("id"))
            if record_id:
                claimed.add(record_id)
            if self._apply_transfer(job, record):
                changed.append(job)
        return changed

    #: How long to wait for slskd to show a transfer we asked for before
    #: deciding it is not coming back.
    MISSING_TRANSFER_TIMEOUT = 90.0

    def _give_up_on_missing(self, job: TransferJob) -> bool:
        """Fail a job slskd has no record of, rather than leaving it stuck.

        slskd forgets transfers when it restarts, and the user can clear them
        from its own interface. Without this the job would sit in Queued for
        ever with no button that could rescue it.
        """
        if job.status is not TransferStatus.QUEUED:
            return False
        submitted = job.payload.get("submitted_at")
        if not isinstance(submitted, (int, float)):
            job.payload["submitted_at"] = time.time()
            return False
        if time.time() - submitted < self.MISSING_TRANSFER_TIMEOUT:
            return False
        job.status = TransferStatus.FAILED
        job.error = "Soulseek no longer has a record of that download."
        job.remedy = (
            "This usually means the helper restarted. Click Retry to ask for the "
            "file again."
        )
        job.touch()
        return True

    def _apply_transfer(self, job: TransferJob, record: dict[str, Any]) -> bool:
        """Copy one slskd transfer record onto our job. Returns True if changed."""
        before = (
            job.status,
            job.downloaded_bytes,
            job.total_bytes,
            job.speed_bps,
            job.error,
            job.eta_seconds,
        )

        remote_state = coerce_str(record.get("state"))
        status_name = classify_transfer_state(remote_state)
        job.status = TransferStatus(status_name)

        total = coerce_int(record.get("size"))
        if total:
            job.total_bytes = total
        transferred = coerce_int(record.get("bytesTransferred"))
        if transferred is not None:
            job.downloaded_bytes = transferred
        speed = record.get("averageSpeed")
        if isinstance(speed, (int, float)):
            job.speed_bps = float(speed)
        remaining = record.get("remainingTime")
        job.eta_seconds = _parse_timespan(remaining)

        transfer_id = coerce_str(record.get("id"))
        if transfer_id:
            job.payload["transfer_id"] = transfer_id
        place = coerce_int(record.get("placeInQueue"))
        if place is not None:
            job.payload["place_in_queue"] = place

        if job.status is TransferStatus.COMPLETE:
            job.downloaded_bytes = job.total_bytes or job.downloaded_bytes
            job.completed_at = job.completed_at or time.time()
            job.error = ""
            job.remedy = ""
        elif job.status is TransferStatus.FAILED:
            detail = coerce_str(record.get("exception"))
            job.error = _explain_transfer_failure(remote_state, detail)
            job.remedy = _remedy_for_transfer_failure(remote_state)

        job.touch()
        after = (
            job.status,
            job.downloaded_bytes,
            job.total_bytes,
            job.speed_bps,
            job.error,
            job.eta_seconds,
        )
        return before != after

    def cancel(self, job: TransferJob) -> bool:
        username = coerce_str(job.payload.get("username"))
        transfer_id = coerce_str(job.payload.get("transfer_id"))
        if not username or not transfer_id:
            job.status = TransferStatus.CANCELLED
            job.touch()
            return True
        try:
            self.ensure_available(allow_start=False)
            ok = self.client.cancel_download(username, transfer_id, remove=True)
        except TrackfetchError:
            ok = False
        job.status = TransferStatus.CANCELLED
        job.touch()
        return ok

    def retry(self, job: TransferJob) -> bool:
        """Ask the same user for the same file again."""
        job.status = TransferStatus.WAITING
        job.error = ""
        job.remedy = ""
        job.payload.pop("transfer_id", None)
        job.touch()
        return True

    # -- diagnostics -------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        data = super().diagnostics()
        try:
            data["slskd"] = self.client.describe()
        except Exception:  # pragma: no cover - defensive
            data["slskd"] = {}
        try:
            data["helper"] = self.supervisor.describe()
        except Exception:  # pragma: no cover - defensive
            data["helper"] = {}
        try:
            data["soulseek_desktop_running"] = self._desktop_client_running()
        except Exception:  # pragma: no cover - defensive
            pass
        return data


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _port_from_url(url: str, default: int = 5030) -> int:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return parsed.port or default
    except ValueError:
        return default


def _parse_timespan(value: Any) -> int | None:
    """slskd reports remaining time as a .NET timespan string or seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = coerce_str(value)
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return int(hours * 3600 + minutes * 60 + seconds)
        if len(parts) == 2:
            return int(float(parts[0]) * 60 + float(parts[1]))
        return int(float(text))
    except ValueError:
        return None


def _explain_transfer_failure(state: str, detail: str) -> str:
    lowered = f"{state} {detail}".lower()
    # Most specific reason first: a rejection that names a missing file is more
    # useful to the person than the generic "refused".
    if "not shared" in lowered or "not found" in lowered or "no such file" in lowered:
        return "That file is no longer being shared."
    if "banned" in lowered or "blocked" in lowered:
        return "That person has blocked downloads from you."
    if "rejected" in lowered:
        return "That person's computer refused the request."
    if "timedout" in lowered or "timed out" in lowered:
        return "That person's computer stopped responding."
    if "connect" in lowered:
        return "Trackfetch could not connect to that person's computer."
    return "The download did not finish."


def _remedy_for_transfer_failure(state: str) -> str:
    return (
        "Soulseek downloads come from other people's computers, so this happens "
        "regularly. Pick a different result for the same item and try again."
    )


def create(ctx: ProviderContext) -> SlskdProvider:
    return SlskdProvider(ctx)
