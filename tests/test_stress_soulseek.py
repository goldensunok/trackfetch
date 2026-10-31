"""A full-size run against a stand-in Soulseek that enforces the real rules.

Everything here goes over a real socket, through the real client, with real
threads. Nothing is patched out. The stand-in reproduces the two behaviours
that caused a wasted afternoon:

* the search timeout is read in milliseconds, so asking for 15 gets you a
  search lasting fifteen thousandths of a second and no results whatsoever;
* the server blocks the account when searches come too fast, and the block
  looks exactly like being disconnected.

The point of the file is to fail loudly here rather than quietly on the user's
machine.
"""

from __future__ import annotations

import time

import pytest

from tests.fake_slskd import FakeSlskdServer
from trackfetch.models.common import BatchItem, InputKind, TransferStatus
from trackfetch.providers.base import ProviderContext
from trackfetch.providers.slskd import SlskdProvider
from trackfetch.services.search_service import SearchService

#: A believable 92 line list, the size that failed in use.
TRACKS = [
    "A Song for You\tDonny Hathaway",
    "Everybody Loves The Sunshine\tRoy Ayers Ubiquity",
    "Someday We'll All Be Free\tDonny Hathaway",
    "This Masquerade - 2000 Remaster\tGeorge Benson",
    "So in Love\tCurtis Mayfield",
    "One Step Ahead\tAretha Franklin",
    "Distant Lover - Live\tMarvin Gaye",
    "The Makings of You\tCurtis Mayfield",
    "Go up Moses\tRoberta Flack",
    "Simply Beautiful\tAl Green",
    "Hope She'll Be Happier\tBill Withers",
    "I Can't Stand the Rain\tAnn Peebles",
    "Les Fleurs\tMinnie Riperton",
    "What Color Is Love\tTerry Callier",
    "You Can't Turn Me Away\tSylvia Striplin",
    "I Got The...\tLabi Siffre",
]
BATCH = [f"{TRACKS[n % len(TRACKS)]} {n}" for n in range(92)]


class _Registry:
    def __init__(self, provider):
        self.provider = provider

    def get(self, provider_id):
        return self.provider if provider_id == "slskd" else None


def _provider(server, settings, secrets):
    provider_settings = settings.provider("slskd")
    provider_settings.enabled = True
    provider_settings.config.update(
        {
            "base_url": server.base_url,
            "username": "slskd",
            "manage_slskd": False,
            "verify_tls": False,
            "timeout": 10,
        }
    )
    ctx = ProviderContext(
        provider_id="slskd",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("password", "testpassword")
    return SlskdProvider(ctx)


def _items(repos, lines):
    items = [BatchItem(raw_text=line, kind=InputKind.QUERY) for line in lines]
    batch_id = repos.batches.create_batch(source="stress")
    repos.batches.add_items(batch_id, items)
    return items


# ---------------------------------------------------------------------------
# The whole list, at a pace the server tolerates
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_full_list_searches_without_being_blocked(repos, settings, secrets):
    """92 items, start to finish, and the account is never blocked.

    The stand-in blocks after 6 searches in 2 seconds. The limiter is set to 4
    in 2 seconds, the same shape as 34 in 220 at a speed a test can wait for.
    """
    with FakeSlskdServer(rate_limit=6, rate_window=2.0, block_seconds=30.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1  # one second of collecting, in real time
        settings.search_concurrency = 2
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 4
        settings.search_rate_window_seconds = 2.0

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH), "slskd")
        provider.close()

        assert service.halted_reason == "", service.halted_reason
        assert len(outcomes) == len(BATCH)
        assert server.world.block_count == 0
        assert server.world.rejected_searches == 0

        found = [o for o in outcomes if o.candidates]
        assert len(found) == len(BATCH), "every search should have returned files"
        assert all(not o.error for o in outcomes)

        # The tab between title and artist must never reach the network.
        assert all("\t" not in t for t in [c.filename for o in outcomes for c in o.candidates])


# ---------------------------------------------------------------------------
# The two faults, reproduced deliberately
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_searching_too_fast_is_what_gets_you_blocked(repos, settings, secrets):
    """Proves the stand-in really does block, so the test above means something."""
    with FakeSlskdServer(rate_limit=6, rate_window=2.0, block_seconds=30.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1
        settings.search_concurrency = 4
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0  # ceiling off: the old behaviour
        settings.search_rate_window_seconds = 2.0

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH), "slskd")
        provider.close()

        assert server.world.block_count >= 1
        # And Trackfetch stops rather than burning the rest of the list.
        assert service.halted_reason
        assert "half an hour" in service.halted_remedy
        assert len(outcomes) < len(BATCH)


@pytest.mark.slow
def test_the_timeout_unit_is_what_makes_results_appear(repos, settings, secrets):
    """A search measured in milliseconds returns nothing, as it did in use."""
    with FakeSlskdServer(rate_limit=1000, rate_window=1.0, first_response_after=0.3) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 2
        settings.search_concurrency = 1
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH[:3]), "slskd")

        assert all(o.candidates for o in outcomes), "two seconds should find files"
        assert server.world.timeouts_seen
        assert all(t == 2000 for t in server.world.timeouts_seen), server.world.timeouts_seen

        # Now the bug as it was: the same number sent as seconds.
        from trackfetch.providers import slskd_client

        original = slskd_client.SlskdClient.search_start

        def as_seconds(self, text, **kwargs):
            kwargs["timeout_seconds"] = kwargs.get("timeout_seconds", 15) / 1000.0
            return original(self, text, **kwargs)

        slskd_client.SlskdClient.search_start = as_seconds
        try:
            service2 = SearchService(_Registry(provider), repos, settings)
            broken = service2.search_all(_items(repos, BATCH[:3]), "slskd")
            assert all(not o.candidates for o in broken), (
                "the stand-in must reproduce the empty results the wrong unit caused, "
                "otherwise the test above proves nothing"
            )
        finally:
            slskd_client.SlskdClient.search_start = original
            provider.close()


# ---------------------------------------------------------------------------
# Choosing files by hand and fetching them
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_choosing_by_hand_then_downloading_the_lot(tmp_path, repos, settings, secrets):
    with FakeSlskdServer(rate_limit=1000, rate_window=1.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1
        settings.search_concurrency = 2
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH[:20]), "slskd")
        assert all(o.candidates for o in outcomes)

        destination = tmp_path / "downloads"
        destination.mkdir()
        jobs = []
        for outcome in outcomes:
            # Deliberately not the top-scoring one: the user's choice wins.
            choice = outcome.candidates[-1]
            jobs.append(provider.submit(provider.build_job(choice, destination)))

        assert len(jobs) == 20
        assert all(j.status is not TransferStatus.FAILED for j in jobs), [
            j.error for j in jobs if j.status is TransferStatus.FAILED
        ]

        deadline = time.time() + 30
        while time.time() < deadline:
            provider.poll(jobs)
            if all(j.status is TransferStatus.COMPLETE for j in jobs):
                break
            time.sleep(0.3)

        provider.close()
        assert all(j.status is TransferStatus.COMPLETE for j in jobs), [
            (j.label, str(j.status), j.error) for j in jobs
        ]


# ---------------------------------------------------------------------------
# The download destination
#
# slskd marks the batch destination [RelativePath] and [NonTraversingPath]:
# relative to its own download folder, and never climbing out of it. Trackfetch
# sent the full Windows path, so every download failed the moment it was asked
# for, with "the Soulseek helper refused the request" and no reason given.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_downloads_are_not_asked_for_with_an_absolute_path(tmp_path, repos, settings, secrets):
    with FakeSlskdServer(rate_limit=1000, rate_window=1.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1
        settings.search_concurrency = 2
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0
        settings.download_dir = str(tmp_path / "music")

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH[:4]), "slskd")
        assert all(o.candidates for o in outcomes)

        # The folder Trackfetch hands the job is the one the user chose.
        destination = tmp_path / "music"
        destination.mkdir(parents=True, exist_ok=True)
        jobs = [provider.submit(provider.build_job(o.candidates[0], destination))
                for o in outcomes]
        provider.close()

        assert server.world.rejected_destinations == [], server.world.rejected_destinations
        assert all(j.status is not TransferStatus.FAILED for j in jobs), [
            j.error for j in jobs if j.status is TransferStatus.FAILED
        ]
        assert all(d is None or not str(d).startswith(("/", "\\"))
                   for d in server.world.destinations_seen)


@pytest.mark.slow
def test_a_subfolder_inside_the_download_folder_is_sent_relative(
    tmp_path, repos, settings, secrets
):
    with FakeSlskdServer(rate_limit=1000, rate_window=1.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1
        settings.search_concurrency = 1
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0
        settings.download_dir = str(tmp_path / "music")

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH[:1]), "slskd")

        inner = tmp_path / "music" / "soul night"
        inner.mkdir(parents=True, exist_ok=True)
        job = provider.submit(provider.build_job(outcomes[0].candidates[0], inner))
        provider.close()

        assert job.status is not TransferStatus.FAILED, job.error
        assert server.world.destinations_seen == ["soul night"]
        assert server.world.rejected_destinations == []


# ---------------------------------------------------------------------------
# slskd only takes two download requests at a time
#
# Its controller holds a semaphore of two and answers anything beyond that with
# 429 immediately. Trackfetch starts three downloads at once by default, so the
# third would fail for a reason no user could act on.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_downloads_started_together_all_get_through(tmp_path, repos, settings, secrets):
    import threading

    with FakeSlskdServer(rate_limit=1000, rate_window=1.0) as server:
        provider = _provider(server, settings, secrets)
        settings.search_timeout = 1
        settings.search_concurrency = 2
        settings.search_gap_seconds = 0.0
        settings.search_rate_limit = 0

        service = SearchService(_Registry(provider), repos, settings)
        outcomes = service.search_all(_items(repos, BATCH[:8]), "slskd")
        assert all(o.candidates for o in outcomes)

        destination = tmp_path / "downloads"
        destination.mkdir()
        jobs = [provider.build_job(o.candidates[0], destination) for o in outcomes]

        # Eight downloads started from several threads at the same moment, the
        # way the queue does it.
        done: list[object] = []
        lock = threading.Lock()

        def submit(job):
            result = provider.submit(job)
            with lock:
                done.append(result)

        threads = [threading.Thread(target=submit, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        provider.close()

        assert len(done) == 8
        failed = [j for j in jobs if j.status is TransferStatus.FAILED]
        assert not failed, [(j.label, j.error) for j in failed]
        # The stand-in must actually have pushed back, or this proves nothing.
        assert server.world.enqueue_429s > 0
