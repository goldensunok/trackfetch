"""One routine that gets Soulseek working, used by both the wizard and Settings.

Steps, in order:

1. Is the helper already answering? If so, use it as it is.
2. Is it installed anywhere sensible? If not, fetch the official release into
   Trackfetch's own folder.
3. Write the helper's configuration (only ever inside Trackfetch's folder).
4. Start it and wait until it answers.
5. Save the details, sign in, and check Soulseek itself is connected.

Nothing here touches the ordinary Soulseek desktop program, or an slskd the user
set up themselves: if one is already running, it is used unchanged.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import string
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from trackfetch.config.secrets import SecretStore
from trackfetch.models.common import ConnectionState, ConnectionStatus

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
ProgressReporter = Callable[[float], None]


@dataclass(slots=True)
class SetupFailure:
    """A setup step that did not work, described the way a person would say it."""

    message: str
    remedy: str


def random_helper_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


def port_from(url: str, default: int = 5030) -> int:
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return parsed.port or default
    except ValueError:
        return default


def ensure_soulseek_ready(
    app,
    soulseek_username: str,
    soulseek_password: str,
    manage_helper: bool = True,
    report: Reporter | None = None,
    progress: ProgressReporter | None = None,
    supervisor=None,
) -> ConnectionStatus | SetupFailure:
    """Get from "nothing set up" to "Soulseek connected", or explain why not."""
    say = report or (lambda message: None)
    tick = progress or (lambda fraction: None)

    from trackfetch.services.slskd_supervisor import SlskdSupervisor

    settings = app.settings.provider("slskd")
    base_url = str(settings.config.get("base_url") or "http://localhost:5030")
    supervisor = supervisor or SlskdSupervisor(port=port_from(base_url))

    say("Checking whether the Soulseek helper is already running")
    already_running = supervisor.is_running(base_url)

    helper_user = str(settings.config.get("username") or "slskd")
    helper_password = app.secrets.get(SecretStore.key_for("slskd", "password"))
    if not helper_password:
        helper_password = random_helper_password()

    if not already_running:
        say("Looking for the Soulseek helper on this computer")
        install = supervisor.find_installation()
        if not install.found:
            say("Not found. Downloading the official helper")
            executable = supervisor.install_latest(
                on_progress=lambda message, fraction: tick(fraction)
            )
            if executable is None:
                return SetupFailure(
                    "Trackfetch could not download the Soulseek helper.",
                    "Check this computer is online and try again. If it still "
                    "fails, you can install slskd yourself and Trackfetch will "
                    "find it.",
                )
            say("Downloaded the Soulseek helper")

        say("Writing the helper's settings")
        try:
            supervisor.write_config(
                soulseek_username=soulseek_username,
                soulseek_password=soulseek_password,
                web_username=helper_user,
                web_password=helper_password,
                downloads_dir=app.settings.resolved_download_dir(),
                port=port_from(base_url),
            )
        except OSError as exc:
            log.warning("could not write the slskd configuration: %s", exc)
            return SetupFailure(
                "Trackfetch could not save the Soulseek helper's settings.",
                "Check there is space on the disk, then try again.",
            )

        say("Starting the Soulseek helper")
        if not supervisor.start():
            return SetupFailure(
                "Trackfetch could not start the Soulseek helper.",
                "Try again. If the problem continues, some security software may "
                "be blocking it the first time it runs.",
            )

        say("Waiting for the helper to be ready")
        if not supervisor.wait_until_ready(base_url, timeout=60):
            return SetupFailure(
                "The Soulseek helper did not finish starting.",
                "Give it another try. Some security software blocks new programs "
                "the first time they run.",
            )

    settings.config.update(
        {
            "base_url": base_url,
            "username": helper_user,
            "manage_slskd": manage_helper,
            "soulseek_username": soulseek_username,
        }
    )
    settings.enabled = True
    app.secrets.set(SecretStore.key_for("slskd", "password"), helper_password)
    app.secrets.set(SecretStore.key_for("slskd", "soulseek_password"), soulseek_password)
    app.save_settings()
    app.refresh_provider("slskd")

    say("Signing in to the helper")
    provider = app.registry.get("slskd")
    if provider is None:
        return SetupFailure(
            "Soulseek support is missing from this copy of Trackfetch.",
            "Use Help then Copy Diagnostic Information and send the result on.",
        )

    say("Checking the Soulseek connection")
    status = provider.test_connection()

    if status.state is not ConnectionState.CONNECTED and already_running:
        say("A helper is already running but Trackfetch could not sign in to it")
    elif status.state is ConnectionState.CONNECTED:
        say("Soulseek connected")
    tick(1.0)
    return status
