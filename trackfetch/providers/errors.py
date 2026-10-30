"""Errors that carry a plain-English message and a plain-English remedy.

Nothing in Trackfetch shows a raw exception to the user. Every failure that can
reach the interface is turned into one of these, which always has:

* ``message``  - what went wrong, in ordinary words;
* ``remedy``   - what the person should do about it;
* ``technical``- the original detail, which goes to the log and the diagnostics
  report only.
"""

from __future__ import annotations


class TrackfetchError(Exception):
    """Base class for every error Trackfetch raises on purpose."""

    default_message = "Something went wrong."
    default_remedy = ""

    def __init__(
        self,
        message: str = "",
        remedy: str = "",
        technical: str = "",
        provider_id: str = "",
    ):
        self.message = message or self.default_message
        self.remedy = remedy or self.default_remedy
        self.technical = technical
        self.provider_id = provider_id
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    def full_text(self) -> str:
        return f"{self.message} {self.remedy}".strip()


class ProviderNotConfigured(TrackfetchError):
    default_message = "This service has not been set up yet."
    default_remedy = "Open Settings, choose the service and click Configure."


class ProviderDisabled(TrackfetchError):
    default_message = "This service is switched off."
    default_remedy = "Open Settings and switch it on."


class ConnectionFailed(TrackfetchError):
    default_message = "Trackfetch could not reach the service."
    default_remedy = "Check the program is running, then click Test Connection."


class AuthenticationFailed(TrackfetchError):
    default_message = "The service rejected the login details."
    default_remedy = "Open Settings and check the username and password."


class RemoteServiceError(TrackfetchError):
    default_message = "The service reported a problem."
    default_remedy = "Try again in a moment. If it keeps happening, restart the program."


class OperationTimedOut(TrackfetchError):
    default_message = "The service took too long to answer."
    default_remedy = "Check it is running and not busy, then try again."


class UnsupportedOperation(TrackfetchError):
    default_message = "This service cannot do that."
    default_remedy = ""


class InvalidResponse(TrackfetchError):
    default_message = "The service sent back something Trackfetch did not understand."
    default_remedy = "Check you are running an up to date version of that program."


class SoulseekNotConnected(TrackfetchError):
    default_message = "Trackfetch is running but it is not signed in to Soulseek."
    default_remedy = (
        "Check your Soulseek username and password in Settings. If you have the "
        "ordinary Soulseek program open, close it and try again, because the "
        "Soulseek network only allows one login per account at a time."
    )


class SlskdUnavailable(TrackfetchError):
    default_message = "Trackfetch cannot currently connect to Soulseek."
    default_remedy = (
        "The background helper that talks to Soulseek is not responding. "
        "Open Settings, choose Soulseek and click Test Connection. Trackfetch "
        "will try to start it for you."
    )


class DestinationError(TrackfetchError):
    default_message = "Trackfetch could not write to the download folder."
    default_remedy = "Open Settings and choose a folder you have permission to write to."
