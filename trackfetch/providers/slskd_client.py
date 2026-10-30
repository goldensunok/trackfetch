"""A client for the slskd HTTP API.

slskd is a headless Soulseek client. Trackfetch never speaks the Soulseek
protocol itself; it drives slskd, which does. The chain is:

    Trackfetch  ->  slskd (running locally)  ->  the Soulseek network

Every path below is slskd's own documented API under ``/api/v0``:

    POST   /session                                  sign in, returns a token
    GET    /session                                  is the token still valid
    GET    /session/enabled                          is authentication switched on
    GET    /application                              full application state
    GET    /application/version                      version string
    GET    /server                                   Soulseek server connection state
    PUT    /server                                   connect to the Soulseek server
    POST   /searches                                 start a search
    GET    /searches/{id}                            search state
    GET    /searches/{id}/responses                  the responses collected so far
    PUT    /searches/{id}                            stop a running search
    DELETE /searches/{id}                            forget a search
    POST   /transfers/downloads/batches              enqueue files (current API)
    POST   /transfers/downloads/{username}           enqueue files (older API)
    GET    /transfers/downloads                      every download
    GET    /transfers/downloads/{username}           one user's downloads
    GET    /transfers/downloads/{username}/{id}      one download
    GET    /transfers/downloads/{username}/{id}/position   place in the remote queue
    DELETE /transfers/downloads/{username}/{id}      cancel, optionally remove
    DELETE /transfers/downloads/all/completed        clear finished entries

Authentication is either an API key sent as ``X-API-Key`` or a username and
password exchanged at ``POST /session`` for a bearer token.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence

import httpx

from trackfetch.models.common import as_list, as_mapping, coerce_bool, coerce_int, coerce_str
from trackfetch.providers._http import HttpApi, normalise_base_url
from trackfetch.providers.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    InvalidResponse,
    RemoteServiceError,
    SlskdUnavailable,
)

log = logging.getLogger(__name__)

API_VERSION = "v0"
SERVICE_NAME = "the Soulseek helper (slskd)"

#: Transfer state strings slskd reports, grouped by what they mean to a person.
TERMINAL_SUCCESS = "Completed, Succeeded"
TERMINAL_CANCELLED = "Completed, Cancelled"
TERMINAL_ERRORED = {
    "Completed, Errored",
    "Completed, TimedOut",
    "Completed, Rejected",
    "Completed, Aborted",
}


class SlskdClient:
    """Synchronous client for one slskd instance.

    Thread safe: a lock guards token refresh, and httpx's client is safe for
    concurrent requests.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5030",
        *,
        api_key: str = "",
        username: str = "",
        password: str = "",
        url_base: str = "",
        verify_tls: bool = True,
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
    ):
        root = normalise_base_url(base_url)
        if url_base:
            root = f"{root}/{url_base.strip('/')}"
        self._root = root
        self._api_root = f"{root}/api/{API_VERSION}" if root else ""
        self._api_key = api_key
        self._username = username
        self._password = password
        self._token = ""
        self._token_expires: float = 0.0
        self._auth_lock = threading.RLock()
        self._batch_endpoint_supported: bool | None = None

        self._http = HttpApi(
            self._api_root,
            service_name=SERVICE_NAME,
            provider_id="slskd",
            verify_tls=verify_tls,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
        if api_key:
            self._http.set_header("X-API-Key", api_key)

    # -- lifecycle ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._root

    @property
    def api_root(self) -> str:
        return self._api_root

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key or (self._username and self._password))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SlskdClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue a request, refreshing the session once if it has expired."""
        response = self._http.request(method, path, raise_for_status=False, **kwargs)
        if response.status_code in (401, 403) and not self._api_key:
            if self._login(force=True):
                response = self._http.request(
                    method, path, raise_for_status=False, **kwargs
                )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE_NAME, "slskd")
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.parse_json(self._request(method, path, **kwargs))

    # -- authentication ----------------------------------------------------

    def security_enabled(self) -> bool:
        """Whether slskd requires authentication at all."""
        try:
            value = self._http.get_json("/session/enabled")
        except Exception:
            return True
        result = coerce_bool(value)
        return True if result is None else result

    def session_valid(self) -> bool:
        try:
            response = self._http.request("GET", "/session", raise_for_status=False)
        except Exception:
            return False
        return response.status_code < 400

    def authenticate(self) -> bool:
        """Make sure the client is able to make authorised calls."""
        if self._api_key:
            return True
        with self._auth_lock:
            if self._token and time.time() < self._token_expires - 30:
                return True
            return self._login()

    def _login(self, force: bool = False) -> bool:
        if self._api_key:
            return True
        if not (self._username and self._password):
            raise AuthenticationFailed(
                "Trackfetch does not have a login for the Soulseek helper.",
                "Open Settings, choose Soulseek and enter the details again.",
                provider_id="slskd",
            )
        with self._auth_lock:
            if not force and self._token and time.time() < self._token_expires - 30:
                return True
            self._http.set_header("Authorization", None)
            response = self._http.request(
                "POST",
                "/session",
                json={"username": self._username, "password": self._password},
                raise_for_status=False,
            )
            if response.status_code in (401, 403):
                raise AuthenticationFailed(
                    "The Soulseek helper rejected Trackfetch's login.",
                    "Open Settings, choose Soulseek and check the details, then "
                    "click Test Connection.",
                    technical=f"HTTP {response.status_code}",
                    provider_id="slskd",
                )
            if response.status_code >= 400:
                from trackfetch.providers._http import translate_status

                raise translate_status(response, SERVICE_NAME, "slskd")
            payload = as_mapping(self._http.parse_json(response))
            token = coerce_str(payload.get("token") or payload.get("accessToken"))
            if not token:
                raise InvalidResponse(
                    "The Soulseek helper did not return a usable login.",
                    "Check you are running a current version of slskd.",
                    technical=f"session response keys: {sorted(payload.keys())}",
                    provider_id="slskd",
                )
            self._token = token
            self._token_expires = self._parse_expiry(payload)
            self._http.set_header("Authorization", f"Bearer {token}")
            return True

    @staticmethod
    def _parse_expiry(payload: Mapping[str, Any]) -> float:
        """Work out when the token stops being valid, conservatively."""
        raw = payload.get("expires")
        if isinstance(raw, (int, float)):
            # Some builds report a unix timestamp, others seconds remaining.
            return float(raw) if raw > 10_000_000 else time.time() + float(raw)
        if isinstance(raw, str) and raw:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = time.strptime(raw.split("+")[0], fmt)
                    return time.mktime(parsed)
                except ValueError:
                    continue
        # Fall back to a short life so the client re-authenticates regularly.
        return time.time() + 3600

    # -- application and server state --------------------------------------

    def ping(self) -> bool:
        """Is anything listening at all? Used before we try to authenticate."""
        try:
            self._http.request("GET", "/session/enabled", raise_for_status=False)
            return True
        except ConnectionFailed:
            return False
        except Exception:
            return True

    def version(self) -> str:
        value = self._json("GET", "/application/version")
        if isinstance(value, str):
            return value
        return coerce_str(as_mapping(value).get("version"))

    def application_state(self) -> dict[str, Any]:
        return dict(as_mapping(self._json("GET", "/application")))

    def server_state(self) -> dict[str, Any]:
        """Soulseek server connection state.

        Newer builds expose ``GET /server``; older ones only report it inside
        ``GET /application``. Both are handled.
        """
        try:
            state = as_mapping(self._json("GET", "/server"))
            if state:
                return dict(state)
        except RemoteServiceError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("server state lookup failed: %r", exc)
        app = self.application_state()
        return dict(as_mapping(app.get("server")))

    def connect_server(self) -> bool:
        try:
            self._request("PUT", "/server")
            return True
        except Exception as exc:
            log.debug("connect to Soulseek server failed: %r", exc)
            return False

    def is_connected_to_soulseek(self) -> tuple[bool, str]:
        """Return (connected, plain description) for the Soulseek link."""
        state = self.server_state()
        raw = coerce_str(state.get("state") or state.get("Status") or "")
        logged_in = coerce_bool(state.get("isLoggedIn"))
        connected = coerce_bool(state.get("isConnected"))
        if logged_in is True or "LoggedIn" in raw:
            return True, "Signed in to Soulseek."
        if connected is True and logged_in is None:
            return True, "Connected to Soulseek."
        if raw:
            return False, f"Soulseek reports: {raw}."
        return False, "Not signed in to Soulseek."

    # -- searches ----------------------------------------------------------

    def search_start(
        self,
        text: str,
        *,
        search_id: str | None = None,
        timeout_seconds: int = 15,
        response_limit: int = 250,
        file_limit: int = 10000,
        filter_responses: bool = True,
        minimum_response_file_count: int = 1,
        maximum_peer_queue_length: int | None = None,
        minimum_peer_upload_speed: int | None = None,
    ) -> dict[str, Any]:
        """Start a real Soulseek search and return slskd's search state."""
        body: dict[str, Any] = {
            "id": search_id or str(uuid.uuid4()),
            "searchText": text,
            "searchTimeout": int(timeout_seconds),
            "responseLimit": int(response_limit),
            "fileLimit": int(file_limit),
            "filterResponses": bool(filter_responses),
            "minimumResponseFileCount": int(minimum_response_file_count),
        }
        if maximum_peer_queue_length is not None:
            body["maximumPeerQueueLength"] = int(maximum_peer_queue_length)
        if minimum_peer_upload_speed is not None:
            body["minimumPeerUploadSpeed"] = int(minimum_peer_upload_speed)
        state = as_mapping(self._json("POST", "/searches", json=body))
        result = dict(state)
        result.setdefault("id", body["id"])
        return result

    def search_state(self, search_id: str, include_responses: bool = False) -> dict[str, Any]:
        params = {"includeResponses": "true"} if include_responses else None
        return dict(as_mapping(self._json("GET", f"/searches/{search_id}", params=params)))

    def search_responses(self, search_id: str) -> list[dict[str, Any]]:
        payload = self._json("GET", f"/searches/{search_id}/responses")
        return [dict(as_mapping(item)) for item in as_list(payload)]

    def search_stop(self, search_id: str) -> bool:
        try:
            self._request("PUT", f"/searches/{search_id}")
            return True
        except Exception as exc:
            log.debug("stopping search %s failed: %r", search_id, exc)
            return False

    def search_delete(self, search_id: str) -> bool:
        try:
            self._request("DELETE", f"/searches/{search_id}")
            return True
        except Exception as exc:
            log.debug("deleting search %s failed: %r", search_id, exc)
            return False

    def search_all(self) -> list[dict[str, Any]]:
        return [dict(as_mapping(i)) for i in as_list(self._json("GET", "/searches"))]

    def run_search(
        self,
        text: str,
        *,
        timeout_seconds: int = 15,
        response_limit: int = 250,
        file_limit: int = 10000,
        minimum_response_file_count: int = 1,
        maximum_peer_queue_length: int | None = None,
        minimum_peer_upload_speed: int | None = None,
        poll_interval: float = 1.0,
        cancel: threading.Event | None = None,
        cleanup: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run one search all the way through.

        Soulseek searches are genuinely asynchronous: slskd broadcasts the query
        and peers answer over the following seconds. This starts the search,
        waits for slskd to mark it complete (or for the timeout to pass), then
        collects the responses. Returns (final state, responses).
        """
        state = self.search_start(
            text,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
            file_limit=file_limit,
            minimum_response_file_count=minimum_response_file_count,
            maximum_peer_queue_length=maximum_peer_queue_length,
            minimum_peer_upload_speed=minimum_peer_upload_speed,
        )
        search_id = coerce_str(state.get("id"))
        if not search_id:
            raise InvalidResponse(
                "The Soulseek helper did not return a search reference.",
                "Check you are running a current version of slskd.",
                technical=f"search state keys: {sorted(state.keys())}",
                provider_id="slskd",
            )

        deadline = time.time() + timeout_seconds + 15
        final = state
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                self.search_stop(search_id)
                break
            time.sleep(poll_interval)
            try:
                final = self.search_state(search_id)
            except Exception as exc:  # transient read problem, keep waiting
                log.debug("search state poll failed: %r", exc)
                continue
            if self._search_finished(final):
                break

        try:
            responses = self.search_responses(search_id)
        except Exception as exc:
            log.debug("collecting responses failed: %r", exc)
            responses = []
        if cleanup:
            self.search_delete(search_id)
        return final, responses

    @staticmethod
    def _search_finished(state: Mapping[str, Any]) -> bool:
        if coerce_bool(state.get("isComplete")) is True:
            return True
        raw = coerce_str(state.get("state"))
        return raw.startswith("Completed") or raw in {"Cancelled", "TimedOut"}

    # -- downloads ---------------------------------------------------------

    def enqueue(
        self,
        username: str,
        files: Sequence[Mapping[str, Any]],
        *,
        destination: str | None = None,
        search_id: str | None = None,
    ) -> bool:
        """Ask slskd to download specific files from one Soulseek user.

        ``files`` entries need ``filename`` and ``size``, exactly as they came
        back from the search. The current API takes a batch; older builds only
        accept the per-user form, so this falls back automatically.
        """
        payload_files = [
            {"filename": coerce_str(f.get("filename")), "size": coerce_int(f.get("size")) or 0}
            for f in files
            if coerce_str(f.get("filename"))
        ]
        if not payload_files:
            raise RemoteServiceError(
                "Trackfetch had nothing to download for that selection.",
                "Try searching again and picking the file once more.",
                provider_id="slskd",
            )

        if self._batch_endpoint_supported is not False:
            body: dict[str, Any] = {"username": username, "files": payload_files}
            if destination:
                body["options"] = {"destination": destination}
            if search_id:
                body["searchId"] = search_id
            response = self._http.request(
                "POST", "/transfers/downloads/batches", json=body, raise_for_status=False
            )
            if response.status_code in (401, 403) and not self._api_key:
                if self._login(force=True):
                    response = self._http.request(
                        "POST",
                        "/transfers/downloads/batches",
                        json=body,
                        raise_for_status=False,
                    )
            if response.status_code < 400:
                self._batch_endpoint_supported = True
                return True
            if response.status_code not in (404, 405):
                from trackfetch.providers._http import translate_status

                raise translate_status(response, SERVICE_NAME, "slskd")
            # Only 404 and 405 mean this build predates the batch endpoint. A
            # 400 is slskd rejecting the request itself, and quietly dropping to
            # the older endpoint would hide that.
            self._batch_endpoint_supported = False
            log.info("slskd batch download endpoint unavailable, using the older API")

        self._request(
            "POST", f"/transfers/downloads/{_quote(username)}", json=payload_files
        )
        return True

    def downloads(self, include_removed: bool = False) -> list[dict[str, Any]]:
        """Every download slskd knows about, flattened into transfer records."""
        params = {"includeRemoved": "true"} if include_removed else None
        payload = self._json("GET", "/transfers/downloads", params=params)
        return flatten_transfers(payload)

    def downloads_for_user(self, username: str) -> list[dict[str, Any]]:
        payload = self._json("GET", f"/transfers/downloads/{_quote(username)}")
        return flatten_transfers(payload, default_username=username)

    def download(self, username: str, transfer_id: str) -> dict[str, Any]:
        return dict(
            as_mapping(
                self._json("GET", f"/transfers/downloads/{_quote(username)}/{transfer_id}")
            )
        )

    def queue_position(self, username: str, transfer_id: str) -> int | None:
        try:
            value = self._json(
                "GET", f"/transfers/downloads/{_quote(username)}/{transfer_id}/position"
            )
        except Exception as exc:
            log.debug("queue position lookup failed: %r", exc)
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return coerce_int(as_mapping(value).get("placeInQueue"))

    def cancel_download(self, username: str, transfer_id: str, remove: bool = False) -> bool:
        params = {"remove": "true"} if remove else None
        try:
            self._request(
                "DELETE",
                f"/transfers/downloads/{_quote(username)}/{transfer_id}",
                params=params,
            )
            return True
        except Exception as exc:
            log.debug("cancelling download failed: %r", exc)
            return False

    def clear_completed_downloads(self) -> bool:
        try:
            self._request("DELETE", "/transfers/downloads/all/completed")
            return True
        except Exception as exc:
            log.debug("clearing completed downloads failed: %r", exc)
            return False

    # -- diagnostics -------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Non-secret facts for the diagnostics report."""
        return {
            "base_url": self._root,
            "api_root": self._api_root,
            "auth_mode": "api-key" if self._api_key else "username/password",
            "has_credentials": self.has_credentials,
            "batch_endpoint_supported": self._batch_endpoint_supported,
        }


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def flatten_transfers(payload: Any, default_username: str = "") -> list[dict[str, Any]]:
    """Turn slskd's nested downloads payload into a flat list of transfers.

    slskd groups downloads as user -> directories -> files. Older builds return a
    flat list. Both shapes are accepted, and anything unrecognised yields an
    empty list rather than an exception.
    """
    out: list[dict[str, Any]] = []

    def add_file(record: Mapping[str, Any], username: str) -> None:
        item = dict(record)
        if username and not item.get("username"):
            item["username"] = username
        if item.get("filename") or item.get("id"):
            out.append(item)

    for entry in as_list(payload):
        node = as_mapping(entry)
        if not node:
            continue
        username = coerce_str(node.get("username")) or default_username
        directories = as_list(node.get("directories"))
        if directories:
            for directory in directories:
                dnode = as_mapping(directory)
                for record in as_list(dnode.get("files")):
                    file_record = as_mapping(record)
                    if file_record:
                        merged = dict(file_record)
                        merged.setdefault("directory", coerce_str(dnode.get("directory")))
                        add_file(merged, username)
            continue
        files = as_list(node.get("files"))
        if files:
            for record in files:
                file_record = as_mapping(record)
                if file_record:
                    add_file(file_record, username)
            continue
        add_file(node, username)
    return out


def classify_transfer_state(state: str) -> str:
    """Map a slskd transfer state string onto a Trackfetch status name."""
    text = (state or "").strip()
    if text == TERMINAL_SUCCESS:
        return "Complete"
    if text == TERMINAL_CANCELLED:
        return "Cancelled"
    if text in TERMINAL_ERRORED:
        return "Failed"
    if text.startswith("Completed"):
        # Any other completed variant that is not success or cancellation.
        return "Failed"
    if "InProgress" in text or text == "Initializing":
        return "Downloading"
    if "Queued" in text or text == "Requested" or text == "None":
        return "Queued"
    if text == "Aborted":
        return "Cancelled"
    return "Queued"


def parse_search_files(
    responses: Iterable[Mapping[str, Any]], include_locked: bool = False
) -> list[dict[str, Any]]:
    """Flatten slskd search responses into one list of file records.

    Each record carries the peer information alongside the file metadata, which
    is what the results table needs.
    """
    out: list[dict[str, Any]] = []
    for response in responses:
        node = as_mapping(response)
        username = coerce_str(node.get("username"))
        peer = {
            "username": username,
            "queueLength": coerce_int(node.get("queueLength")),
            "hasFreeUploadSlot": coerce_bool(node.get("hasFreeUploadSlot")),
            "uploadSpeed": coerce_int(node.get("uploadSpeed")),
            "token": coerce_int(node.get("token")),
        }
        groups: list[tuple[str, bool]] = [("files", False)]
        if include_locked:
            groups.append(("lockedFiles", True))
        for key, locked in groups:
            for raw_file in as_list(node.get(key)):
                file_node = as_mapping(raw_file)
                filename = coerce_str(file_node.get("filename"))
                if not filename:
                    continue
                out.append(
                    {
                        "filename": filename,
                        "size": coerce_int(file_node.get("size")),
                        "extension": coerce_str(file_node.get("extension")),
                        "bitRate": coerce_int(file_node.get("bitRate")),
                        "bitDepth": coerce_int(file_node.get("bitDepth")),
                        "sampleRate": coerce_int(file_node.get("sampleRate")),
                        "length": coerce_int(file_node.get("length")),
                        "isVariableBitRate": coerce_bool(file_node.get("isVariableBitRate")),
                        "code": coerce_int(file_node.get("code")),
                        "locked": locked,
                        "peer": peer,
                    }
                )
    return out


def split_soulseek_path(path: str) -> tuple[str, str]:
    """Split a Soulseek path into (directory, filename).

    Soulseek paths are Windows style and come from arbitrary strangers, so this
    tolerates both separators and never raises.
    """
    text = coerce_str(path)
    if not text:
        return "", ""
    index = max(text.rfind("\\"), text.rfind("/"))
    if index < 0:
        return "", text
    return text[:index], text[index + 1 :]


def slskd_unavailable_error(technical: str = "") -> SlskdUnavailable:
    return SlskdUnavailable(technical=technical, provider_id="slskd")
