"""Configuration forms, described once and built automatically.

Each provider declares the handful of things it needs, in ordinary words, with
a note explaining where to find the value. Secrets are marked, and those go to
the operating system credential store rather than the settings file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FieldKind = Literal["text", "password", "int", "bool", "choice", "folder"]


@dataclass(slots=True)
class FormField:
    key: str
    label: str
    kind: FieldKind = "text"
    secret: bool = False
    default: Any = ""
    hint: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)
    minimum: int = 0
    maximum: int = 65535
    required: bool = False
    #: Advanced fields are tucked away behind a collapsed section, so an
    #: ordinary setup shows only the two or three things that actually matter.
    advanced: bool = False


@dataclass(slots=True)
class ProviderForm:
    provider_id: str
    title: str
    #: One or two sentences: what this is and what it is for.
    description: str
    #: Where to find the program, if the user has not got it.
    where_to_get: str = ""
    fields: list[FormField] = field(default_factory=list)
    #: True when the provider keeps a list of saved servers rather than one.
    multi_server: bool = False


COMMON_TLS = FormField(
    key="verify_tls",
    label="Check security certificates",
    kind="bool",
    default=True,
    hint="Leave this ticked unless you are connecting to your own server with a "
    "self-signed certificate.",
    advanced=True,
)


PROVIDER_FORMS: dict[str, ProviderForm] = {
    "slskd": ProviderForm(
        provider_id="slskd",
        title="Soulseek",
        description=(
            "Soulseek lets you search files that other people are sharing. "
            "Trackfetch talks to Soulseek through a small background helper "
            "called slskd, which it can install and start for you."
        ),
        where_to_get="Trackfetch can download the helper for you in the setup guide.",
        fields=[
            FormField(
                "soulseek_username",
                "Your Soulseek username",
                required=True,
                hint="The same username you use in the ordinary Soulseek program, "
                "or a second account.",
            ),
            FormField(
                "soulseek_password",
                "Your Soulseek password",
                kind="password",
                secret=True,
                required=True,
            ),
            FormField(
                "manage_slskd",
                "Let Trackfetch start the helper automatically",
                kind="bool",
                default=True,
                hint="Recommended. Turn this off only if you run slskd yourself.",
            ),
            FormField(
                "base_url",
                "Helper address",
                default="http://localhost:5030",
                hint="Leave this as it is unless you run slskd on another computer.",
                advanced=True,
            ),
            FormField(
                "username",
                "Helper username",
                default="slskd",
                hint="The login for the helper itself, not for Soulseek. Trackfetch "
                "sets this up for you.",
                advanced=True,
            ),
            FormField(
                "password", "Helper password", kind="password", secret=True, advanced=True
            ),
            FormField(
                "api_key",
                "Helper key (optional)",
                kind="password",
                secret=True,
                hint="Only needed if you set up slskd yourself with an API key.",
                advanced=True,
            ),
            COMMON_TLS,
        ],
    ),
    "http": ProviderForm(
        provider_id="http",
        title="Web downloads",
        description=(
            "Downloads a file straight from a web address. Nothing to set up."
        ),
        fields=[
            FormField(
                "timeout",
                "Give up after (seconds)",
                kind="int",
                default=60,
                minimum=5,
                maximum=3600,
            ),
            COMMON_TLS,
        ],
    ),
    "qbittorrent": ProviderForm(
        provider_id="qbittorrent",
        title="qBittorrent",
        description=(
            "Sends torrents and magnet links to qBittorrent, which you already "
            "have installed, and shows their progress here."
        ),
        where_to_get="In qBittorrent: Tools, Options, Web UI. Switch on the Web "
        "user interface and note the port, username and password.",
        fields=[
            FormField(
                "base_url",
                "Address",
                default="http://localhost:8080",
                required=True,
                hint="The Web UI address shown in qBittorrent's options.",
            ),
            FormField("username", "Username", default="admin"),
            FormField("password", "Password", kind="password", secret=True),
            COMMON_TLS,
        ],
    ),
    "transmission": ProviderForm(
        provider_id="transmission",
        title="Transmission",
        description="Sends torrents and magnet links to Transmission.",
        where_to_get="In Transmission: Edit, Preferences, Remote. Switch on remote "
        "access and note the port.",
        fields=[
            FormField(
                "base_url",
                "Address",
                default="http://localhost:9091/transmission/rpc",
                required=True,
            ),
            FormField("username", "Username"),
            FormField("password", "Password", kind="password", secret=True),
            COMMON_TLS,
        ],
    ),
    "deluge": ProviderForm(
        provider_id="deluge",
        title="Deluge",
        description="Sends torrents and magnet links to Deluge's web interface.",
        where_to_get="Start deluge-web, then open it in a browser once and set a "
        "password.",
        fields=[
            FormField("base_url", "Address", default="http://localhost:8112", required=True),
            FormField("password", "Web password", kind="password", secret=True, required=True),
            COMMON_TLS,
        ],
    ),
    "jdownloader": ProviderForm(
        provider_id="jdownloader",
        title="JDownloader",
        description=(
            "Hands web addresses to JDownloader, which knows how to handle a great "
            "many file hosting sites."
        ),
        where_to_get="In JDownloader: Settings, My.JDownloader. Sign in there with "
        "the same email and password you enter here.",
        fields=[
            FormField("email", "My.JDownloader email", required=True),
            FormField(
                "password",
                "My.JDownloader password",
                kind="password",
                secret=True,
                required=True,
            ),
            FormField(
                "device_name",
                "Which computer (optional)",
                hint="Leave this empty to use the first JDownloader on your account.",
            ),
            COMMON_TLS,
        ],
    ),
    "aria2": ProviderForm(
        provider_id="aria2",
        title="aria2",
        description=(
            "A small, fast downloader that handles web addresses, magnet links and "
            "torrent files."
        ),
        where_to_get="Start it with: aria2c --enable-rpc --rpc-listen-all",
        fields=[
            FormField(
                "base_url",
                "Address",
                default="http://localhost:6800/jsonrpc",
                required=True,
            ),
            FormField("secret", "Secret token", kind="password", secret=True),
            COMMON_TLS,
        ],
    ),
    "sftp": ProviderForm(
        provider_id="sftp",
        title="SFTP servers",
        description=(
            "Connects to servers you have an account on, so you can browse them and "
            "download files."
        ),
        multi_server=True,
        fields=[
            FormField("name", "A name for this server", required=True, default="My server"),
            FormField("host", "Address", required=True),
            FormField("port", "Port", kind="int", default=22, minimum=1, maximum=65535),
            FormField("username", "Username", required=True),
            FormField("password", "Password", kind="password", secret=True),
            FormField(
                "key_file",
                "Key file (optional)",
                hint="Only if you sign in with a key file instead of a password.",
            ),
            FormField("start_path", "Start in folder", default="."),
        ],
    ),
    "webdav": ProviderForm(
        provider_id="webdav",
        title="WebDAV",
        description=(
            "Connects to a WebDAV server, such as Nextcloud or ownCloud, so you can "
            "browse it and download files."
        ),
        fields=[
            FormField(
                "base_url",
                "Address",
                required=True,
                hint="For Nextcloud this usually ends in /remote.php/dav/files/yourname/",
            ),
            FormField("username", "Username"),
            FormField("password", "Password", kind="password", secret=True),
            COMMON_TLS,
        ],
    ),
    "syncthing": ProviderForm(
        provider_id="syncthing",
        title="Syncthing",
        description=(
            "Shows the folders and devices you have already set up in Syncthing, and "
            "lets you pull files from them. It is not a way to search the internet."
        ),
        where_to_get="In Syncthing: Actions, Settings, General. Copy the API key.",
        fields=[
            FormField("base_url", "Address", default="http://localhost:8384", required=True),
            FormField("api_key", "API key", kind="password", secret=True, required=True),
            COMMON_TLS,
        ],
    ),
    "nzbget": ProviderForm(
        provider_id="nzbget",
        title="NZBGet",
        description="Sends NZB files to NZBGet and shows their progress here.",
        where_to_get="In NZBGet: Settings, Security. Note the control username and "
        "password.",
        fields=[
            FormField("base_url", "Address", default="http://localhost:6789", required=True),
            FormField("username", "Username", default="nzbget"),
            FormField("password", "Password", kind="password", secret=True),
            COMMON_TLS,
        ],
    ),
    "sabnzbd": ProviderForm(
        provider_id="sabnzbd",
        title="SABnzbd",
        description="Sends NZB files to SABnzbd and shows their progress here.",
        where_to_get="In SABnzbd: Config, General. Copy the API key.",
        fields=[
            FormField("base_url", "Address", default="http://localhost:8080", required=True),
            FormField("api_key", "API key", kind="password", secret=True, required=True),
            COMMON_TLS,
        ],
    ),
}


def form_for(provider_id: str) -> ProviderForm | None:
    return PROVIDER_FORMS.get(provider_id)
