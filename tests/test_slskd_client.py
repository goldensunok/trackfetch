"""Tests for the slskd HTTP client.

Every test mocks slskd's HTTP boundary with the payload shapes its documented
API returns. This proves Trackfetch calls the right endpoints with the right
bodies and understands the replies. It does not prove a live Soulseek
connection, which is verified separately on the user's own machine.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
import respx

from tests.fixtures import slskd_payloads as fx
from trackfetch.providers.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    InvalidResponse,
    OperationTimedOut,
    RemoteServiceError,
)
from trackfetch.providers.slskd_client import (
    SlskdClient,
    classify_transfer_state,
    flatten_transfers,
    parse_search_files,
    split_soulseek_path,
)

BASE = "http://localhost:5030"
API = f"{BASE}/api/v0"


@pytest.fixture
def client():
    c = SlskdClient(BASE, username="slskd", password="hunter2", timeout=2, connect_timeout=1)
    yield c
    c.close()


@pytest.fixture
def apikey_client():
    c = SlskdClient(BASE, api_key="0123456789abcdef0123", timeout=2, connect_timeout=1)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# URL and construction
# ---------------------------------------------------------------------------


def test_api_root_is_the_documented_v0_path():
    c = SlskdClient("localhost:5030")
    assert c.api_root == "http://localhost:5030/api/v0"
    c.close()


def test_url_base_is_honoured():
    c = SlskdClient("https://box.example.com", url_base="slskd")
    assert c.api_root == "https://box.example.com/slskd/api/v0"
    c.close()


def test_api_key_is_sent_as_the_documented_header(apikey_client):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{API}/application").mock(
            return_value=httpx.Response(200, json=fx.APPLICATION_STATE)
        )
        apikey_client.application_state()
    assert route.calls[0].request.headers["X-API-Key"] == "0123456789abcdef0123"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@respx.mock
def test_login_posts_credentials_and_stores_bearer_token(client):
    route = respx.post(f"{API}/session").mock(
        return_value=httpx.Response(200, json=fx.SESSION_LOGIN)
    )
    assert client.authenticate() is True
    body = json.loads(route.calls[0].request.content)
    assert body == {"username": "slskd", "password": "hunter2"}

    state = respx.get(f"{API}/application").mock(
        return_value=httpx.Response(200, json=fx.APPLICATION_STATE)
    )
    client.application_state()
    assert state.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
def test_login_rejected_raises_a_readable_error(client):
    respx.post(f"{API}/session").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(AuthenticationFailed) as exc:
        client.authenticate()
    assert "rejected" in exc.value.message.lower()
    assert "Settings" in exc.value.remedy


@respx.mock
def test_login_without_a_token_in_the_reply_is_reported_clearly(client):
    respx.post(f"{API}/session").mock(return_value=httpx.Response(200, json={"name": "x"}))
    with pytest.raises(InvalidResponse):
        client.authenticate()


@respx.mock
def test_expired_session_triggers_one_silent_reauthentication(client):
    respx.post(f"{API}/session").mock(
        return_value=httpx.Response(200, json=fx.SESSION_LOGIN)
    )
    responses = [
        httpx.Response(401, text="expired"),
        httpx.Response(200, json=fx.APPLICATION_STATE),
    ]
    respx.get(f"{API}/application").mock(side_effect=responses)
    state = client.application_state()
    assert state["server"]["isLoggedIn"] is True


@respx.mock
def test_slskd_not_running_is_reported_as_a_connection_problem(client):
    respx.get(f"{API}/session/enabled").mock(side_effect=httpx.ConnectError("refused"))
    assert client.ping() is False


@respx.mock
def test_timeout_is_translated(client):
    respx.post(f"{API}/session").mock(
        return_value=httpx.Response(200, json=fx.SESSION_LOGIN)
    )
    respx.get(f"{API}/application").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OperationTimedOut):
        client.application_state()


# ---------------------------------------------------------------------------
# Soulseek connection state
# ---------------------------------------------------------------------------


@respx.mock
def test_soulseek_connected_is_detected(apikey_client):
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_CONNECTED)
    )
    connected, description = apikey_client.is_connected_to_soulseek()
    assert connected is True
    assert "Soulseek" in description


@respx.mock
def test_soulseek_disconnected_is_detected(apikey_client):
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    connected, description = apikey_client.is_connected_to_soulseek()
    assert connected is False
    assert "Disconnected" in description


@respx.mock
def test_server_state_falls_back_to_application_state_on_older_builds(apikey_client):
    respx.get(f"{API}/server").mock(return_value=httpx.Response(404, text="not found"))
    respx.get(f"{API}/application").mock(
        return_value=httpx.Response(200, json=fx.APPLICATION_STATE)
    )
    connected, _ = apikey_client.is_connected_to_soulseek()
    assert connected is True


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@respx.mock
def test_search_start_posts_the_documented_body(apikey_client):
    route = respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    apikey_client.search_start("Aphex Twin - Xtal", timeout_seconds=20, response_limit=100)
    body = json.loads(route.calls[0].request.content)
    assert body["searchText"] == "Aphex Twin - Xtal"
    # Milliseconds, not seconds, whatever the documentation says. See the note
    # in search_start and the regression test that guards it.
    assert body["searchTimeout"] == 20000
    assert body["responseLimit"] == 100
    assert "id" in body
    assert body["fileLimit"] == 10000
    assert body["filterResponses"] is True


@respx.mock
def test_run_search_waits_for_completion_then_collects_responses(apikey_client):
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        side_effect=[
            httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS),
            httpx.Response(200, json=fx.SEARCH_STATE_COMPLETE),
        ]
    )
    responses_route = respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_RESPONSES)
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))

    state, responses = apikey_client.run_search(
        "Aphex Twin - Xtal", timeout_seconds=3, poll_interval=0.01
    )
    assert state["isComplete"] is True
    assert len(responses) == 3
    assert responses_route.called


@respx.mock
def test_run_search_can_be_cancelled_midway(apikey_client):
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    stop = respx.put(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(204)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))

    cancel = threading.Event()
    cancel.set()
    _, responses = apikey_client.run_search(
        "anything", timeout_seconds=5, poll_interval=0.01, cancel=cancel
    )
    assert responses == []
    assert stop.called


@respx.mock
def test_search_uses_our_own_id_when_the_reply_omits_it(apikey_client):
    route = respx.post(f"{API}/searches").mock(return_value=httpx.Response(200, json={}))
    state = apikey_client.search_start("something")
    sent_id = json.loads(route.calls[0].request.content)["id"]
    assert state["id"] == sent_id


@respx.mock
def test_malformed_json_is_reported_readably(apikey_client):
    respx.get(f"{API}/application").mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    with pytest.raises(InvalidResponse) as exc:
        apikey_client.application_state()
    assert "could not read" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# Parsing search responses
# ---------------------------------------------------------------------------


def test_parse_search_files_flattens_every_user_and_file():
    records = parse_search_files(fx.SEARCH_RESPONSES)
    assert len(records) == 3
    usernames = {r["peer"]["username"] for r in records}
    assert usernames == {"peer_one", "peer_two"}


def test_parse_search_files_can_include_locked_files():
    records = parse_search_files(fx.SEARCH_RESPONSES, include_locked=True)
    assert len(records) == 4
    locked = [r for r in records if r["locked"]]
    assert len(locked) == 1
    assert locked[0]["peer"]["username"] == "peer_three"


def test_parse_search_files_exposes_all_available_metadata():
    record = parse_search_files(fx.SEARCH_RESPONSES)[0]
    assert record["size"] == 28311552
    assert record["extension"] == "flac"
    assert record["bitRate"] == 1004
    assert record["bitDepth"] == 16
    assert record["sampleRate"] == 44100
    assert record["length"] == 293
    assert record["isVariableBitRate"] is False
    assert record["peer"]["queueLength"] == 0
    assert record["peer"]["hasFreeUploadSlot"] is True
    assert record["peer"]["uploadSpeed"] == 812345


def test_parse_search_files_tolerates_rubbish():
    assert parse_search_files([]) == []
    assert parse_search_files([{"username": "x"}]) == []
    assert parse_search_files([{"username": "x", "files": "not a list"}]) == []
    assert parse_search_files([{"files": [{"size": 1}]}]) == []


def test_split_soulseek_path_handles_both_separators():
    assert split_soulseek_path("C:\\a\\b\\c.mp3") == ("C:\\a\\b", "c.mp3")
    assert split_soulseek_path("/home/x/y.flac") == ("/home/x", "y.flac")
    assert split_soulseek_path("bare.mp3") == ("", "bare.mp3")
    assert split_soulseek_path("") == ("", "")


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------


@respx.mock
def test_enqueue_uses_the_batch_endpoint_when_available(apikey_client):
    route = respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(201)
    )
    ok = apikey_client.enqueue(
        "peer_one",
        [{"filename": "C:\\x\\y.flac", "size": 123}],
        destination="D:\\Music",
    )
    assert ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["username"] == "peer_one"
    assert body["files"] == [{"filename": "C:\\x\\y.flac", "size": 123}]
    assert body["options"]["destination"] == "D:\\Music"


@respx.mock
def test_enqueue_falls_back_to_the_older_endpoint(apikey_client):
    respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(404, text="not found")
    )
    legacy = respx.post(f"{API}/transfers/downloads/peer_one").mock(
        return_value=httpx.Response(201)
    )
    assert apikey_client.enqueue("peer_one", [{"filename": "a.mp3", "size": 5}]) is True
    body = json.loads(legacy.calls[0].request.content)
    assert body == [{"filename": "a.mp3", "size": 5}]


@respx.mock
def test_enqueue_remembers_the_older_endpoint_for_later_calls(apikey_client):
    respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(404)
    )
    legacy = respx.post(f"{API}/transfers/downloads/peer_one").mock(
        return_value=httpx.Response(201)
    )
    apikey_client.enqueue("peer_one", [{"filename": "a.mp3", "size": 5}])
    apikey_client.enqueue("peer_one", [{"filename": "b.mp3", "size": 6}])
    assert len(legacy.calls) == 2


@respx.mock
def test_enqueue_with_nothing_selected_is_refused(apikey_client):
    with pytest.raises(RemoteServiceError):
        apikey_client.enqueue("peer_one", [])


@respx.mock
def test_enqueue_quotes_awkward_usernames(apikey_client):
    respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(404)
    )
    route = respx.post(f"{API}/transfers/downloads/odd%20user%2Fname").mock(
        return_value=httpx.Response(201)
    )
    apikey_client.enqueue("odd user/name", [{"filename": "a.mp3", "size": 1}])
    assert route.called


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


@respx.mock
def test_downloads_flattens_the_grouped_shape(apikey_client):
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_GROUPED)
    )
    transfers = apikey_client.downloads()
    assert len(transfers) == 1
    assert transfers[0]["username"] == "peer_one"
    assert transfers[0]["bytesTransferred"] == 14155776


def test_flatten_transfers_accepts_the_flat_shape_too():
    assert len(flatten_transfers(fx.DOWNLOADS_FLAT)) == 1


def test_flatten_transfers_tolerates_rubbish():
    assert flatten_transfers(None) == []
    assert flatten_transfers("nonsense") == []
    assert flatten_transfers([{"directories": "bad"}]) == []


@respx.mock
def test_cancel_download_calls_the_documented_endpoint(apikey_client):
    route = respx.delete(f"{API}/transfers/downloads/peer_one/abc").mock(
        return_value=httpx.Response(204)
    )
    assert apikey_client.cancel_download("peer_one", "abc", remove=True) is True
    assert "remove=true" in str(route.calls[0].request.url)


@respx.mock
def test_queue_position_is_read(apikey_client):
    respx.get(f"{API}/transfers/downloads/peer_two/abc/position").mock(
        return_value=httpx.Response(200, json=7)
    )
    assert apikey_client.queue_position("peer_two", "abc") == 7


@pytest.mark.parametrize(
    "state,expected",
    [
        ("Completed, Succeeded", "Complete"),
        ("Completed, Cancelled", "Cancelled"),
        ("Completed, Errored", "Failed"),
        ("Completed, TimedOut", "Failed"),
        ("Completed, Rejected", "Failed"),
        ("InProgress", "Downloading"),
        ("Initializing", "Downloading"),
        ("Queued, Remotely", "Queued"),
        ("Queued, Locally", "Queued"),
        ("Requested", "Queued"),
        ("None", "Queued"),
        ("Aborted", "Cancelled"),
        ("", "Queued"),
    ],
)
def test_transfer_state_classification(state, expected):
    assert classify_transfer_state(state) == expected
