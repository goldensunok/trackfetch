# Trackfetch

A multi-provider file search, retrieval and download manager with a desktop
interface. One understandable window over twelve different download mechanisms,
built on a capability-based provider architecture.

If you are the person who is going to *use* Trackfetch rather than work on it,
read **README_FOR_TOBIAS.txt** instead. It assumes no programming knowledge.

## What it does

Soulseek is the primary provider. Trackfetch drives a locally running
[slskd](https://github.com/slskd/slskd) over its documented HTTP API and never
implements the Soulseek protocol itself:

```
Trackfetch  ->  slskd (local)  ->  the Soulseek network
```

The workflow the application is built around:

```
paste or import a list
        |
   SEARCH ALL          real Soulseek searches, progress shown as "Searching 17 / 126"
        |
inspect and filter     user-controlled filters, no built-in preferences
        |
tick what you want     nothing is ever selected automatically
        |
DOWNLOAD SELECTED      only ticked files are fetched
        |
one unified queue      progress, pause, resume, retry, cancel
```

## Providers

| Provider | Mechanism | Capabilities |
| --- | --- | --- |
| Soulseek (slskd) | slskd HTTP API `/api/v0` | SEARCH, ADD_JOB, QUEUE, PROGRESS, CANCEL, AUTH, CONNECTION_TEST |
| Web download | HTTP/HTTPS, performed in-process | RESOLVE, DOWNLOAD, PROGRESS, PAUSE, RESUME, CANCEL |
| qBittorrent | Web UI API v2 | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |
| Transmission | RPC with session-id handshake | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |
| Deluge | web JSON-RPC `/json` | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |
| JDownloader | My.JDownloader signed and encrypted API | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |
| aria2 | JSON-RPC | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL |
| SFTP | Paramiko, performed in-process | BROWSE, DOWNLOAD, PROGRESS, RESUME, CANCEL, AUTH |
| WebDAV | PROPFIND and ranged GET, in-process | BROWSE, DOWNLOAD, PROGRESS, RESUME, CANCEL, AUTH |
| Syncthing | REST API, then a local copy | BROWSE, DOWNLOAD, PROGRESS, AUTH |
| NZBGet | JSON-RPC | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |
| SABnzbd | HTTP API | ADD_JOB, QUEUE, PROGRESS, PAUSE, RESUME, CANCEL, AUTH |

See `docs/PROVIDER_MATRIX.md` for exactly what has been tested and how.

## Design notes

**Capability-based, not lowest common denominator.** Providers advertise a set
of capabilities and the interface adapts. Searching Soulseek and handing a
magnet link to qBittorrent are genuinely different operations and are not forced
into one pretend workflow.

**No taste is encoded.** Relevance scoring measures textual similarity and
nothing else. There is no rule preferring studio over live, original over remix,
one release over another, or any format or bitrate. Those are user decisions,
made with the filters and the sort controls. `tests/test_filters_and_relevance.py`
asserts this directly.

**Search and download are separate.** Nothing is ever downloaded because it
scored highly. `DOWNLOAD SELECTED` transfers exactly what was ticked.

**Plain-English failures.** Every error that can reach the interface carries a
`message`, a `remedy` and a `technical` field. Only the first two are shown; the
third goes to the log and the diagnostic report.

**Secrets stay out of everything.** Passwords, keys and tokens live in the
operating system credential store. A redaction filter runs over every log
record, and the diagnostic report strips credentials from URLs and hides
sensitive keys. Tested in `tests/test_security_and_diagnostics.py`.

## Layout

```
trackfetch/
    config/         settings, paths, credential storage
    models/         shared value types, no dependencies
    providers/      one adapter per external mechanism, plus the registry
    services/       input parsing, relevance, filters, search, queue, slskd setup
    storage/        thread-safe SQLite and the repositories
    diagnostics/    logging with redaction, the diagnostic report
    gui/            PySide6 windows, dialogs and panels
tests/              325 automated tests
packaging/          PyInstaller spec, Inno Setup script, version metadata
.github/workflows/  the Windows build
```

## Running from source

```bash
python -m pip install -e ".[dev]"
python -m trackfetch
```

Tests:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests -q
```

`TRACKFETCH_HOME` redirects every application path into one folder, which is how
the tests keep out of the way of a real installation.

## Building the Windows installer

Push to GitHub and the workflow in `.github/workflows/build-windows.yml` runs
the tests, builds `Trackfetch.exe` with PyInstaller and packages
`TrackfetchSetup.exe` with Inno Setup. The installer appears as a downloadable
artifact on the run page. See `docs/GETTING_THE_INSTALLER.md` for the
click-by-click version.

Locally on Windows:

```powershell
pip install -e . pyinstaller
pyinstaller packaging/trackfetch.spec --noconfirm --clean
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

## Licence

MIT.
