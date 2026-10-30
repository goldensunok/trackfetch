"""Tests for the providers that drive another program.

Each one is exercised against its own documented API, mocked at the HTTP
boundary with the shapes that program really returns. These prove Trackfetch
speaks each API correctly. They are not live tests of those programs.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from trackfetch.models.common import (
    ConnectionState,
    SearchCandidate,
    TransferJob,
    TransferStatus,
)
from trackfetch.providers.base import ProviderContext


def make_provider(klass, settings, secrets, provider_id, config=None, secret_values=None):
    provider_settings = settings.provider(provider_id)
    provider_settings.enabled = True
    provider_settings.config.update(config or {})
    ctx = ProviderContext(
        provider_id=provider_id,
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    for key, value in (secret_values or {}).items():
        ctx.set_secret(key, value)
    return klass(ctx)


def job(provider_id: str, source: str, tmp_path: Path, remote_id: str = "") -> TransferJob:
    return TransferJob(
        provider_id=provider_id,
        label="thing",
        destination=str(tmp_path),
        remote_id=remote_id,
        payload={"source": source},
    )


# ===========================================================================
# qBittorrent
# ===========================================================================

QB = "http://localhost:8080/api/v2"


@pytest.fixture
def qbittorrent(settings, secrets):
    from trackfetch.providers.qbittorrent import QBittorrentProvider

    p = make_provider(
        QBittorrentProvider,
        settings,
        secrets,
        "qbittorrent",
        {"base_url": "http://localhost:8080", "username": "admin"},
        {"password": "adminadmin"},
    )
    yield p
    p.close()


def _qb_login():
    respx.post(f"{QB}/auth/login").mock(return_value=httpx.Response(200, text="Ok."))


@respx.mock
def test_qbittorrent_connects(qbittorrent):
    _qb_login()
    respx.get(f"{QB}/app/version").mock(return_value=httpx.Response(200, text="v5.0.3"))
    status = qbittorrent.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "v5.0.3" in status.detail


@respx.mock
def test_qbittorrent_bad_password_reads_plainly(qbittorrent):
    respx.post(f"{QB}/auth/login").mock(return_value=httpx.Response(200, text="Fails."))
    status = qbittorrent.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "rejected" in status.detail.lower()
    assert "Settings" in status.remedy


@respx.mock
def test_qbittorrent_not_running_reads_plainly(qbittorrent):
    respx.post(f"{QB}/auth/login").mock(side_effect=httpx.ConnectError("refused"))
    status = qbittorrent.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "cannot reach" in status.detail.lower()


@respx.mock
def test_qbittorrent_accepts_a_magnet(qbittorrent, tmp_path):
    _qb_login()
    respx.get(f"{QB}/torrents/info").mock(return_value=httpx.Response(200, json=[]))
    add = respx.post(f"{QB}/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
    magnet = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=thing"
    result = qbittorrent.submit(job("qbittorrent", magnet, tmp_path))
    assert result.status is TransferStatus.QUEUED
    assert result.remote_id == "abcdef0123456789abcdef0123456789abcdef01"
    assert b"savepath" in add.calls[0].request.content


@respx.mock
def test_qbittorrent_accepts_a_torrent_file(qbittorrent, tmp_path):
    _qb_login()
    torrent = tmp_path / "x.torrent"
    torrent.write_bytes(b"d8:announce")
    respx.get(f"{QB}/torrents/info").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[{"hash": "deadbeef", "name": "X"}]),
        ]
    )
    respx.post(f"{QB}/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
    result = qbittorrent.submit(job("qbittorrent", str(torrent), tmp_path))
    assert result.remote_id == "deadbeef"
    assert result.label == "X"


@respx.mock
def test_qbittorrent_missing_torrent_file_fails_cleanly(qbittorrent, tmp_path):
    _qb_login()
    result = qbittorrent.submit(job("qbittorrent", str(tmp_path / "gone.torrent"), tmp_path))
    assert result.status is TransferStatus.FAILED
    assert "could not find" in result.error


@respx.mock
def test_qbittorrent_reports_progress_and_completion(qbittorrent, tmp_path):
    _qb_login()
    respx.get(f"{QB}/torrents/info").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "hash": "abc",
                    "name": "Big File",
                    "state": "downloading",
                    "size": 1000,
                    "completed": 400,
                    "dlspeed": 2048,
                    "eta": 300,
                    "save_path": "/downloads",
                }
            ],
        )
    )
    j = job("qbittorrent", "magnet:?x", tmp_path, remote_id="abc")
    changed = qbittorrent.poll([j])
    assert changed == [j]
    assert j.status is TransferStatus.DOWNLOADING
    assert j.downloaded_bytes == 400
    assert j.speed_bps == 2048.0
    assert j.eta_seconds == 300
    assert j.label == "Big File"


@respx.mock
@pytest.mark.parametrize(
    "state,expected",
    [
        ("uploading", TransferStatus.COMPLETE),
        ("pausedUP", TransferStatus.COMPLETE),
        ("stoppedUP", TransferStatus.COMPLETE),
        ("pausedDL", TransferStatus.PAUSED),
        ("stoppedDL", TransferStatus.PAUSED),
        ("queuedDL", TransferStatus.QUEUED),
        ("error", TransferStatus.FAILED),
        ("missingFiles", TransferStatus.FAILED),
    ],
)
def test_qbittorrent_state_mapping(qbittorrent, tmp_path, state, expected):
    _qb_login()
    respx.get(f"{QB}/torrents/info").mock(
        return_value=httpx.Response(
            200, json=[{"hash": "abc", "state": state, "size": 10, "completed": 10}]
        )
    )
    j = job("qbittorrent", "magnet:?x", tmp_path, remote_id="abc")
    qbittorrent.poll([j])
    assert j.status is expected


@respx.mock
def test_qbittorrent_pause_uses_the_new_name_and_falls_back(qbittorrent, tmp_path):
    _qb_login()
    respx.post(f"{QB}/torrents/stop").mock(return_value=httpx.Response(404, text="no"))
    legacy = respx.post(f"{QB}/torrents/pause").mock(return_value=httpx.Response(200))
    j = job("qbittorrent", "magnet:?x", tmp_path, remote_id="abc")
    assert qbittorrent.pause(j) is True
    assert legacy.called
    assert j.status is TransferStatus.PAUSED


@respx.mock
def test_qbittorrent_cancel_does_not_delete_the_files(qbittorrent, tmp_path):
    _qb_login()
    route = respx.post(f"{QB}/torrents/delete").mock(return_value=httpx.Response(200))
    j = job("qbittorrent", "magnet:?x", tmp_path, remote_id="abc")
    assert qbittorrent.cancel(j) is True
    assert b"deleteFiles=false" in route.calls[0].request.content


# ===========================================================================
# Transmission
# ===========================================================================

TR = "http://localhost:9091/transmission/rpc"


@pytest.fixture
def transmission(settings, secrets):
    from trackfetch.providers.transmission import TransmissionProvider

    p = make_provider(
        TransmissionProvider, settings, secrets, "transmission", {"base_url": TR}
    )
    yield p
    p.close()


@respx.mock
def test_transmission_does_the_session_id_handshake(transmission):
    respx.post(TR).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "abc123"}),
            httpx.Response(200, json={"result": "success", "arguments": {"version": "4.0.5"}}),
        ]
    )
    status = transmission.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "4.0.5" in status.detail


@respx.mock
def test_transmission_adds_a_magnet(transmission, tmp_path):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "torrent-add":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "arguments": {"torrent-added": {"id": 7, "name": "Thing", "hashString": "h"}},
                },
            )
        return httpx.Response(200, json={"result": "success", "arguments": {}})

    respx.post(TR).mock(side_effect=handler)
    result = transmission.submit(job("transmission", "magnet:?xt=urn:btih:abc", tmp_path))
    assert result.remote_id == "7"
    assert result.label == "Thing"
    assert calls[0]["arguments"]["filename"].startswith("magnet:")
    assert calls[0]["arguments"]["download-dir"] == str(tmp_path)


@respx.mock
def test_transmission_adds_a_torrent_file_as_base64(transmission, tmp_path):
    torrent = tmp_path / "x.torrent"
    torrent.write_bytes(b"d8:announce")
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(
            200,
            json={"result": "success", "arguments": {"torrent-added": {"id": 1, "name": "x"}}},
        )

    respx.post(TR).mock(side_effect=handler)
    transmission.submit(job("transmission", str(torrent), tmp_path))
    assert "metainfo" in captured["arguments"]


@respx.mock
def test_transmission_reports_progress(transmission, tmp_path):
    respx.post(TR).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "success",
                "arguments": {
                    "torrents": [
                        {
                            "id": 7,
                            "name": "Thing",
                            "status": 4,
                            "sizeWhenDone": 1000,
                            "leftUntilDone": 250,
                            "rateDownload": 512,
                            "eta": 60,
                            "downloadDir": "/dl",
                            "error": 0,
                        }
                    ]
                },
            },
        )
    )
    j = job("transmission", "magnet:?x", tmp_path, remote_id="7")
    transmission.poll([j])
    assert j.status is TransferStatus.DOWNLOADING
    assert j.downloaded_bytes == 750
    assert j.eta_seconds == 60
    assert j.destination == "/dl"


@respx.mock
def test_transmission_reports_an_error_readably(transmission, tmp_path):
    respx.post(TR).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "success",
                "arguments": {
                    "torrents": [
                        {
                            "id": 7,
                            "status": 4,
                            "error": 3,
                            "errorString": "unregistered torrent",
                            "sizeWhenDone": 10,
                            "leftUntilDone": 10,
                        }
                    ]
                },
            },
        )
    )
    j = job("transmission", "magnet:?x", tmp_path, remote_id="7")
    transmission.poll([j])
    assert j.status is TransferStatus.FAILED
    assert "unregistered" not in j.error
    assert j.remedy


@respx.mock
def test_transmission_rejects_a_bad_login(transmission):
    respx.post(TR).mock(return_value=httpx.Response(401))
    status = transmission.test_connection()
    assert "rejected" in status.detail.lower()


# ===========================================================================
# Deluge
# ===========================================================================

DL = "http://localhost:8112/json"


@pytest.fixture
def deluge(settings, secrets):
    from trackfetch.providers.deluge import DelugeProvider

    p = make_provider(
        DelugeProvider,
        settings,
        secrets,
        "deluge",
        {"base_url": "http://localhost:8112"},
        {"password": "deluge"},
    )
    yield p
    p.close()


def _deluge_router(responses: dict):
    def handler(request):
        body = json.loads(request.content)
        method = body["method"]
        if method not in responses:
            return httpx.Response(200, json={"result": None, "error": None, "id": body["id"]})
        value = responses[method]
        if isinstance(value, dict) and "error" in value:
            return httpx.Response(200, json={"result": None, "error": value["error"], "id": 1})
        return httpx.Response(200, json={"result": value, "error": None, "id": 1})

    return handler


@respx.mock
def test_deluge_connects(deluge):
    respx.post(DL).mock(
        side_effect=_deluge_router(
            {
                "auth.login": True,
                "web.connected": True,
                "daemon.get_version": "2.1.1",
            }
        )
    )
    status = deluge.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "2.1.1" in status.detail


@respx.mock
def test_deluge_rejects_a_bad_password(deluge):
    respx.post(DL).mock(side_effect=_deluge_router({"auth.login": False}))
    status = deluge.test_connection()
    assert "rejected" in status.detail.lower()


@respx.mock
def test_deluge_connects_the_daemon_when_the_web_page_is_not(deluge):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["method"])
        results = {
            "auth.login": True,
            "web.connected": False,
            "web.get_hosts": [["hostid", "127.0.0.1", 58846, "Online"]],
            "web.connect": None,
            "daemon.get_version": "2.1.1",
        }
        return httpx.Response(
            200, json={"result": results.get(body["method"]), "error": None, "id": 1}
        )

    respx.post(DL).mock(side_effect=handler)
    deluge.test_connection()
    assert "web.connect" in calls


@respx.mock
def test_deluge_adds_a_magnet_and_reports_progress(deluge, tmp_path):
    respx.post(DL).mock(
        side_effect=_deluge_router(
            {
                "auth.login": True,
                "web.connected": True,
                "core.add_torrent_magnet": "torrentid1",
                "core.get_torrents_status": {
                    "torrentid1": {
                        "name": "Thing",
                        "state": "Downloading",
                        "total_wanted": 2000,
                        "total_done": 500,
                        "download_payload_rate": 1024,
                        "eta": 90,
                        "save_path": "/dl",
                    }
                },
            }
        )
    )
    result = deluge.submit(job("deluge", "magnet:?xt=urn:btih:abc", tmp_path))
    assert result.remote_id == "torrentid1"
    deluge.poll([result])
    assert result.status is TransferStatus.DOWNLOADING
    assert result.downloaded_bytes == 500
    assert result.label == "Thing"


# ===========================================================================
# aria2
# ===========================================================================

AR = "http://localhost:6800/jsonrpc"


@pytest.fixture
def aria2(settings, secrets):
    from trackfetch.providers.aria2 import Aria2Provider

    p = make_provider(
        Aria2Provider, settings, secrets, "aria2", {"base_url": AR}, {"secret": "sss"}
    )
    yield p
    p.close()


@respx.mock
def test_aria2_connects_and_sends_the_token(aria2):
    route = respx.post(AR).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {"version": "1.37.0"}}
        )
    )
    status = aria2.test_connection()
    assert status.state is ConnectionState.CONNECTED
    body = json.loads(route.calls[0].request.content)
    assert body["params"][0] == "token:sss"
    assert body["method"] == "aria2.getVersion"


@respx.mock
def test_aria2_rejects_a_bad_token(aria2):
    respx.post(AR).mock(
        return_value=httpx.Response(
            200, json={"error": {"code": 1, "message": "Unauthorized"}, "id": "1"}
        )
    )
    status = aria2.test_connection()
    assert "rejected" in status.detail.lower()


@respx.mock
def test_aria2_adds_a_link_and_reports_progress(aria2, tmp_path):
    respx.post(AR).mock(
        side_effect=[
            httpx.Response(200, json={"result": "gid123", "id": "1"}),
            httpx.Response(
                200,
                json={
                    "result": {
                        "gid": "gid123",
                        "status": "active",
                        "totalLength": "1000",
                        "completedLength": "600",
                        "downloadSpeed": "200",
                        "files": [{"path": "/dl/thing.iso"}],
                    },
                    "id": "2",
                },
            ),
        ]
    )
    j = aria2.submit(job("aria2", "https://example.com/thing.iso", tmp_path))
    assert j.remote_id == "gid123"
    aria2.poll([j])
    assert j.status is TransferStatus.DOWNLOADING
    assert j.downloaded_bytes == 600
    assert j.eta_seconds == 2
    assert j.label == "thing.iso"


@respx.mock
def test_aria2_reports_an_error(aria2, tmp_path):
    respx.post(AR).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {"gid": "g", "status": "error", "errorMessage": "404"},
                "id": "1",
            },
        )
    )
    j = job("aria2", "https://x", tmp_path, remote_id="g")
    aria2.poll([j])
    assert j.status is TransferStatus.FAILED
    assert j.remedy


# ===========================================================================
# NZBGet
# ===========================================================================

NG = "http://localhost:6789/jsonrpc"


@pytest.fixture
def nzbget(settings, secrets):
    from trackfetch.providers.nzbget import NzbgetProvider

    p = make_provider(
        NzbgetProvider,
        settings,
        secrets,
        "nzbget",
        {"base_url": "http://localhost:6789", "username": "nzbget"},
        {"password": "tegbzn6789"},
    )
    yield p
    p.close()


def _nzbget_router(results: dict):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"version": "1.1", "result": results.get(body["method"])})

    return handler


@respx.mock
def test_nzbget_connects(nzbget):
    respx.post(NG).mock(
        side_effect=_nzbget_router({"version": "21.1", "status": {"ServerStandBy": True}})
    )
    status = nzbget.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "21.1" in status.detail


@respx.mock
def test_nzbget_rejects_a_bad_login(nzbget):
    respx.post(NG).mock(return_value=httpx.Response(401))
    status = nzbget.test_connection()
    assert "rejected" in status.detail.lower()


@respx.mock
def test_nzbget_appends_an_nzb_with_the_documented_parameter_order(nzbget, tmp_path):
    nzb = tmp_path / "thing.nzb"
    nzb.write_bytes(b"<nzb></nzb>")
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(200, json={"result": 42})

    respx.post(NG).mock(side_effect=handler)
    result = nzbget.submit(job("nzbget", str(nzb), tmp_path))
    assert result.remote_id == "42"
    assert result.status is TransferStatus.QUEUED
    params = captured["params"]
    assert params[0] == "thing.nzb"
    assert len(params) == 10
    assert params[8] == "SCORE"


@respx.mock
def test_nzbget_reports_a_refusal(nzbget, tmp_path):
    nzb = tmp_path / "thing.nzb"
    nzb.write_bytes(b"<nzb></nzb>")
    respx.post(NG).mock(return_value=httpx.Response(200, json={"result": 0}))
    result = nzbget.submit(job("nzbget", str(nzb), tmp_path))
    assert result.status is TransferStatus.FAILED
    assert "did not accept" in result.error


@respx.mock
def test_nzbget_reports_progress_from_the_queue(nzbget, tmp_path):
    respx.post(NG).mock(
        side_effect=_nzbget_router(
            {
                "listgroups": [
                    {
                        "NZBID": 42,
                        "NZBName": "Thing",
                        "Status": "DOWNLOADING",
                        "FileSizeLo": 1000,
                        "FileSizeHi": 0,
                        "RemainingSizeLo": 250,
                        "RemainingSizeHi": 0,
                        "DestDir": "/dl/thing",
                    }
                ],
                "history": [],
            }
        )
    )
    j = job("nzbget", "x.nzb", tmp_path, remote_id="42")
    nzbget.poll([j])
    assert j.status is TransferStatus.DOWNLOADING
    assert j.total_bytes == 1000
    assert j.downloaded_bytes == 750
    assert j.label == "Thing"


@respx.mock
def test_nzbget_reports_completion_from_the_history(nzbget, tmp_path):
    respx.post(NG).mock(
        side_effect=_nzbget_router(
            {
                "listgroups": [],
                "history": [{"NZBID": 42, "Status": "SUCCESS/ALL", "FinalDir": "/done"}],
            }
        )
    )
    j = job("nzbget", "x.nzb", tmp_path, remote_id="42")
    nzbget.poll([j])
    assert j.status is TransferStatus.COMPLETE
    assert j.destination == "/done"


@respx.mock
def test_nzbget_reports_failure_from_the_history(nzbget, tmp_path):
    respx.post(NG).mock(
        side_effect=_nzbget_router(
            {"listgroups": [], "history": [{"NZBID": 42, "Status": "FAILURE/PAR"}]}
        )
    )
    j = job("nzbget", "x.nzb", tmp_path, remote_id="42")
    nzbget.poll([j])
    assert j.status is TransferStatus.FAILED
    assert j.remedy


# ===========================================================================
# SABnzbd
# ===========================================================================

SAB = "http://localhost:8080/api"


@pytest.fixture
def sabnzbd(settings, secrets):
    from trackfetch.providers.sabnzbd import SabnzbdProvider

    p = make_provider(
        SabnzbdProvider,
        settings,
        secrets,
        "sabnzbd",
        {"base_url": "http://localhost:8080"},
        {"api_key": "KEY123"},
    )
    yield p
    p.close()


@respx.mock
def test_sabnzbd_connects_and_sends_the_key(sabnzbd):
    route = respx.get(SAB).mock(return_value=httpx.Response(200, json={"version": "4.3.0"}))
    status = sabnzbd.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "apikey=KEY123" in str(route.calls[0].request.url)


@respx.mock
def test_sabnzbd_rejects_a_bad_key(sabnzbd):
    respx.get(SAB).mock(return_value=httpx.Response(200, text="API Key Incorrect"))
    status = sabnzbd.test_connection()
    assert "rejected" in status.detail.lower()


@respx.mock
def test_sabnzbd_uploads_an_nzb(sabnzbd, tmp_path):
    nzb = tmp_path / "thing.nzb"
    nzb.write_bytes(b"<nzb></nzb>")
    respx.post(SAB).mock(
        return_value=httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})
    )
    result = sabnzbd.submit(job("sabnzbd", str(nzb), tmp_path))
    assert result.remote_id == "SABnzbd_nzo_1"
    assert result.status is TransferStatus.QUEUED


@respx.mock
def test_sabnzbd_reports_progress(sabnzbd, tmp_path):
    respx.get(SAB).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "queue": {
                        "slots": [
                            {
                                "nzo_id": "n1",
                                "filename": "Thing",
                                "status": "Downloading",
                                "mb": "100",
                                "mbleft": "40",
                                "timeleft": "0:01:30",
                            }
                        ]
                    }
                },
            ),
            httpx.Response(200, json={"history": {"slots": []}}),
        ]
    )
    j = job("sabnzbd", "x.nzb", tmp_path, remote_id="n1")
    sabnzbd.poll([j])
    assert j.status is TransferStatus.DOWNLOADING
    assert j.total_bytes == 100 * 1024 * 1024
    assert j.downloaded_bytes == 60 * 1024 * 1024
    assert j.eta_seconds == 90


@respx.mock
def test_sabnzbd_reports_a_failure_from_the_history(sabnzbd, tmp_path):
    respx.get(SAB).mock(
        side_effect=[
            httpx.Response(200, json={"queue": {"slots": []}}),
            httpx.Response(
                200,
                json={
                    "history": {
                        "slots": [
                            {
                                "nzo_id": "n1",
                                "status": "Failed",
                                "fail_message": "Not enough articles",
                            }
                        ]
                    }
                },
            ),
        ]
    )
    j = job("sabnzbd", "x.nzb", tmp_path, remote_id="n1")
    sabnzbd.poll([j])
    assert j.status is TransferStatus.FAILED
    assert j.remedy


# ===========================================================================
# Syncthing
# ===========================================================================

ST = "http://localhost:8384"


@pytest.fixture
def syncthing(settings, secrets):
    from trackfetch.providers.syncthing import SyncthingProvider

    p = make_provider(
        SyncthingProvider,
        settings,
        secrets,
        "syncthing",
        {"base_url": ST},
        {"api_key": "STKEY"},
    )
    yield p
    p.close()


@respx.mock
def test_syncthing_connects_and_counts_folders(syncthing, tmp_path):
    route = respx.get(f"{ST}/rest/system/version").mock(
        return_value=httpx.Response(200, json={"version": "v1.27.0"})
    )
    respx.get(f"{ST}/rest/config/folders").mock(
        return_value=httpx.Response(
            200, json=[{"id": "music", "label": "Music", "path": str(tmp_path)}]
        )
    )
    respx.get(f"{ST}/rest/system/connections").mock(
        return_value=httpx.Response(200, json={"connections": {"DEV1": {"connected": True}}})
    )
    status = syncthing.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "1 folder" in status.detail
    assert route.calls[0].request.headers["X-API-Key"] == "STKEY"


@respx.mock
def test_syncthing_lists_folders_then_files(syncthing, tmp_path):
    respx.get(f"{ST}/rest/config/folders").mock(
        return_value=httpx.Response(
            200, json=[{"id": "music", "label": "Music", "path": str(tmp_path)}]
        )
    )
    folders = syncthing.browse("")
    assert folders[0].name == "Music"
    assert folders[0].path == "music:"

    respx.get(f"{ST}/rest/db/browse").mock(
        return_value=httpx.Response(
            200,
            json={
                "album": {},
                "track.flac": ["2026-01-01T00:00:00Z", 1234],
            },
        )
    )
    entries = syncthing.browse("music:")
    names = {e.name: e for e in entries}
    assert names["album"].is_dir is True
    assert names["track.flac"].size == 1234
    assert names["track.flac"].extra["local_path"].endswith("track.flac")


@respx.mock
def test_syncthing_copies_a_file_out(syncthing, tmp_path):
    source_dir = tmp_path / "synced"
    source_dir.mkdir()
    source = source_dir / "track.flac"
    source.write_bytes(b"a" * 5000)
    target_dir = tmp_path / "downloads"
    target_dir.mkdir()

    j = TransferJob(
        provider_id="syncthing",
        label="track.flac",
        destination=str(target_dir),
        payload={"local_path": str(source), "target_path": str(target_dir / "track.flac")},
    )
    syncthing.download(j)
    assert j.status is TransferStatus.COMPLETE
    assert (target_dir / "track.flac").read_bytes() == b"a" * 5000


def test_syncthing_explains_when_the_file_is_not_here_yet(syncthing, tmp_path):
    j = TransferJob(
        provider_id="syncthing",
        destination=str(tmp_path),
        payload={"local_path": str(tmp_path / "missing.flac")},
    )
    syncthing.download(j)
    assert j.status is TransferStatus.FAILED
    assert "not on this computer yet" in j.error
    assert "synchronising" in j.remedy


def test_syncthing_is_not_configured_without_a_key(settings, secrets):
    from trackfetch.providers.syncthing import SyncthingProvider

    p = make_provider(SyncthingProvider, settings, secrets, "syncthing", {"base_url": ST})
    assert p.is_configured() is False
    assert p.test_connection().state is ConnectionState.NOT_CONFIGURED
    p.close()
