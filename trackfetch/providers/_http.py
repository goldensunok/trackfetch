"""Shared HTTP plumbing for the providers that talk to a local web API.

Most of the programs Trackfetch drives (slskd, qBittorrent, Transmission,
JDownloader, NZBGet, SABnzbd, Deluge, aria2, Syncthing, WebDAV) expose an HTTP
interface. This module gives them one consistent client with:

* sensible connect/read timeouts, so nothing hangs the interface forever;
* TLS verification on by default;
* JSON parsing that never lets a malformed reply escape as a raw exception;
* translation of every network failure into a plain-English Trackfetch error.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from trackfetch.providers.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    InvalidResponse,
    OperationTimedOut,
    RemoteServiceError,
)

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0


def normalise_base_url(url: str, default_scheme: str = "http") -> str:
    """Turn whatever the user typed into a usable base URL.

    Accepts "localhost:5030", "http://localhost:5030/", "https://box/slskd" and
    so on. A person setting this up should not have to know about schemes.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def host_and_port(url: str) -> tuple[str, int | None]:
    parsed = urlparse(normalise_base_url(url))
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname or "", port


def redact_url(url: str) -> str:
    """Strip credentials and obvious API keys so a URL is safe to log."""
    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover - defensive
        return "(unparseable url)"
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    query = parsed.query
    if query:
        parts = []
        for chunk in query.split("&"):
            key = chunk.split("=", 1)[0]
            if key.lower() in {"apikey", "api_key", "key", "token", "password", "pass"}:
                parts.append(f"{key}=***")
            else:
                parts.append(chunk)
        query = "?" + "&".join(parts)
    return f"{parsed.scheme}://{netloc}{parsed.path}{query}"


def translate_http_error(
    exc: Exception, service_name: str, provider_id: str = ""
) -> Exception:
    """Convert an httpx or socket failure into a Trackfetch error."""
    if isinstance(exc, httpx.TimeoutException):
        return OperationTimedOut(
            f"{service_name} took too long to answer.",
            f"Check {service_name} is running and not busy, then try again.",
            technical=repr(exc),
            provider_id=provider_id,
        )
    if isinstance(exc, httpx.ConnectError):
        return ConnectionFailed(
            f"Trackfetch cannot reach {service_name}.",
            f"Check that {service_name} is running and that the address in "
            "Settings is correct.",
            technical=repr(exc),
            provider_id=provider_id,
        )
    if isinstance(exc, httpx.TransportError):
        return ConnectionFailed(
            f"The connection to {service_name} was lost.",
            f"Check {service_name} is still running, then try again.",
            technical=repr(exc),
            provider_id=provider_id,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return translate_status(exc.response, service_name, provider_id)
    return RemoteServiceError(
        f"{service_name} reported an unexpected problem.",
        "Try again in a moment.",
        technical=repr(exc),
        provider_id=provider_id,
    )


def translate_status(
    response: httpx.Response, service_name: str, provider_id: str = ""
) -> Exception:
    code = response.status_code
    detail = ""
    try:
        detail = response.text[:400]
    except Exception:  # pragma: no cover - defensive
        pass
    if code in (401, 403):
        return AuthenticationFailed(
            f"{service_name} rejected the login details.",
            "Open Settings, choose that service and check the username, password "
            "or key.",
            technical=f"HTTP {code}: {detail}",
            provider_id=provider_id,
        )
    if code == 404:
        return RemoteServiceError(
            f"{service_name} did not recognise that request.",
            f"You may be running an older version of {service_name}. Updating it "
            "usually fixes this.",
            technical=f"HTTP {code}: {detail}",
            provider_id=provider_id,
        )
    if 500 <= code < 600:
        return RemoteServiceError(
            f"{service_name} had an internal problem.",
            f"Restarting {service_name} usually clears this.",
            technical=f"HTTP {code}: {detail}",
            provider_id=provider_id,
        )
    return RemoteServiceError(
        f"{service_name} refused the request.",
        "Check the settings for that service.",
        technical=f"HTTP {code}: {detail}",
        provider_id=provider_id,
    )


class HttpApi:
    """A thin, synchronous HTTP client shared by the API-backed providers."""

    def __init__(
        self,
        base_url: str,
        *,
        service_name: str,
        provider_id: str = "",
        headers: Mapping[str, str] | None = None,
        verify_tls: bool = True,
        timeout: float = DEFAULT_READ_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        auth: tuple[str, str] | None = None,
        follow_redirects: bool = True,
    ):
        self.base_url = normalise_base_url(base_url)
        self.service_name = service_name
        self.provider_id = provider_id
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._client = httpx.Client(
            headers=dict(headers or {}),
            verify=verify_tls,
            timeout=self._timeout,
            follow_redirects=follow_redirects,
            auth=auth,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "HttpApi":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def client(self) -> httpx.Client:
        return self._client

    def set_header(self, name: str, value: str | None) -> None:
        if value is None:
            self._client.headers.pop(name, None)
        else:
            self._client.headers[name] = value

    # -- requests ----------------------------------------------------------

    def url_for(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if not path or path == "/":
            # An empty path means the base URL itself. Some RPC endpoints are a
            # single address and adding a trailing slash breaks them.
            return self.base_url
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        url = self.url_for(path)
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            log.debug("%s %s failed: %r", method, redact_url(url), exc)
            raise translate_http_error(exc, self.service_name, self.provider_id) from exc
        if raise_for_status and response.status_code >= 400:
            raise translate_status(response, self.service_name, self.provider_id)
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    # -- JSON helpers ------------------------------------------------------

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        return self.parse_json(response)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.json("GET", path, **kwargs)

    def post_json(self, path: str, **kwargs: Any) -> Any:
        return self.json("POST", path, **kwargs)

    def parse_json(self, response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            snippet = response.text[:200] if response.text else ""
            raise InvalidResponse(
                f"{self.service_name} sent a reply Trackfetch could not read.",
                f"Check you are running a current version of {self.service_name}.",
                technical=f"invalid JSON: {snippet}",
                provider_id=self.provider_id,
            ) from exc
