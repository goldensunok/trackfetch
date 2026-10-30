"""Realistic slskd API payloads used by the test suite.

These mirror the shapes slskd's documented API actually returns. They are
fixtures for the HTTP boundary. Passing tests against them prove Trackfetch
speaks slskd's API correctly; they do NOT prove a live Soulseek connection
works, which can only be verified on a machine with slskd running.
"""

from __future__ import annotations

SESSION_LOGIN = {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token",
    "expires": "2099-01-01T00:00:00.000Z",
    "name": "slskd",
}

APPLICATION_STATE = {
    "version": {"full": "0.26.0", "current": "0.26.0", "isUpdateAvailable": False},
    "server": {
        "address": "vps.slsknet.org",
        "port": 2271,
        "isConnected": True,
        "isLoggedIn": True,
        "state": "Connected, LoggedIn",
        "username": "tobias",
    },
    "user": {"username": "tobias", "privileges": {"isPrivileged": False}},
    "rooms": [],
    "shares": {"directories": 0, "files": 0, "scanning": False},
}

APPLICATION_STATE_DISCONNECTED = {
    "version": {"full": "0.26.0"},
    "server": {
        "address": "vps.slsknet.org",
        "port": 2271,
        "isConnected": False,
        "isLoggedIn": False,
        "state": "Disconnected",
    },
}

SERVER_STATE_CONNECTED = {
    "address": "vps.slsknet.org",
    "port": 2271,
    "isConnected": True,
    "isLoggedIn": True,
    "state": "Connected, LoggedIn",
    "username": "tobias",
}

SERVER_STATE_DISCONNECTED = {
    "address": "vps.slsknet.org",
    "port": 2271,
    "isConnected": False,
    "isLoggedIn": False,
    "state": "Disconnected",
}

SEARCH_ID = "0c0f2a94-6f0e-4d5f-9d1a-6d2b4a7f1c3e"

SEARCH_STATE_IN_PROGRESS = {
    "id": SEARCH_ID,
    "searchText": "Aphex Twin - Xtal",
    "token": 1234567,
    "state": "InProgress",
    "isComplete": False,
    "fileCount": 0,
    "lockedFileCount": 0,
    "responseCount": 0,
    "startedAt": "2026-08-18T10:00:00.000Z",
}

SEARCH_STATE_COMPLETE = {
    "id": SEARCH_ID,
    "searchText": "Aphex Twin - Xtal",
    "token": 1234567,
    "state": "Completed, TimedOut",
    "isComplete": True,
    "fileCount": 4,
    "lockedFileCount": 1,
    "responseCount": 3,
    "startedAt": "2026-08-18T10:00:00.000Z",
    "endedAt": "2026-08-18T10:00:15.000Z",
}

SEARCH_RESPONSES = [
    {
        "username": "peer_one",
        "token": 1234567,
        "hasFreeUploadSlot": True,
        "queueLength": 0,
        "uploadSpeed": 812345,
        "fileCount": 2,
        "lockedFileCount": 0,
        "files": [
            {
                "code": 1,
                "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                "size": 28311552,
                "extension": "flac",
                "bitRate": 1004,
                "bitDepth": 16,
                "sampleRate": 44100,
                "length": 293,
                "isVariableBitRate": False,
            },
            {
                "code": 1,
                "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\02 Tha.flac",
                "size": 31457280,
                "extension": "flac",
                "bitRate": 998,
                "bitDepth": 16,
                "sampleRate": 44100,
                "length": 543,
                "isVariableBitRate": False,
            },
        ],
        "lockedFiles": [],
    },
    {
        "username": "peer_two",
        "token": 1234567,
        "hasFreeUploadSlot": False,
        "queueLength": 12,
        "uploadSpeed": 51200,
        "fileCount": 1,
        "lockedFileCount": 0,
        "files": [
            {
                "code": 1,
                "filename": "C:\\shared\\aphex_twin-xtal.mp3",
                "size": 7340032,
                "extension": "mp3",
                "bitRate": 320,
                "bitDepth": None,
                "sampleRate": 44100,
                "length": 297,
                "isVariableBitRate": False,
            }
        ],
        "lockedFiles": [],
    },
    {
        "username": "peer_three",
        "token": 1234567,
        "hasFreeUploadSlot": True,
        "queueLength": 3,
        "uploadSpeed": 220000,
        "fileCount": 0,
        "lockedFileCount": 1,
        "files": [],
        "lockedFiles": [
            {
                "code": 1,
                "filename": "\\private\\Aphex Twin - Xtal (live at Warehouse).mp3",
                "size": 8912896,
                "extension": "mp3",
                "bitRate": 192,
                "sampleRate": 44100,
                "length": 310,
                "isVariableBitRate": True,
            }
        ],
    },
]

#: The grouped shape GET /transfers/downloads returns: user -> directories -> files.
DOWNLOADS_GROUPED = [
    {
        "username": "peer_one",
        "directories": [
            {
                "directory": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92",
                "fileCount": 1,
                "files": [
                    {
                        "id": "6f1d1f1c-1111-2222-3333-444455556666",
                        "username": "peer_one",
                        "direction": "Download",
                        "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                        "size": 28311552,
                        "startOffset": 0,
                        "state": "InProgress",
                        "requestedAt": "2026-08-18T10:01:00.000Z",
                        "enqueuedAt": "2026-08-18T10:01:01.000Z",
                        "startedAt": "2026-08-18T10:01:05.000Z",
                        "bytesTransferred": 14155776,
                        "averageSpeed": 524288.0,
                        "bytesRemaining": 14155776,
                        "percentComplete": 50.0,
                        "remainingTime": "00:00:27.0000000",
                        "placeInQueue": None,
                    }
                ],
            }
        ],
    }
]

DOWNLOADS_COMPLETE = [
    {
        "username": "peer_one",
        "directories": [
            {
                "directory": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92",
                "fileCount": 1,
                "files": [
                    {
                        "id": "6f1d1f1c-1111-2222-3333-444455556666",
                        "username": "peer_one",
                        "direction": "Download",
                        "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                        "size": 28311552,
                        "state": "Completed, Succeeded",
                        "bytesTransferred": 28311552,
                        "averageSpeed": 611000.0,
                        "percentComplete": 100.0,
                    }
                ],
            }
        ],
    }
]

DOWNLOADS_FAILED = [
    {
        "username": "peer_two",
        "directories": [
            {
                "directory": "C:\\shared",
                "fileCount": 1,
                "files": [
                    {
                        "id": "aaaa1111-2222-3333-4444-555566667777",
                        "username": "peer_two",
                        "direction": "Download",
                        "filename": "C:\\shared\\aphex_twin-xtal.mp3",
                        "size": 7340032,
                        "state": "Completed, Errored",
                        "bytesTransferred": 1048576,
                        "exception": "The remote user rejected the request: File not shared.",
                    }
                ],
            }
        ],
    }
]

DOWNLOADS_QUEUED_REMOTELY = [
    {
        "username": "peer_two",
        "directories": [
            {
                "directory": "C:\\shared",
                "fileCount": 1,
                "files": [
                    {
                        "id": "aaaa1111-2222-3333-4444-555566667777",
                        "username": "peer_two",
                        "direction": "Download",
                        "filename": "C:\\shared\\aphex_twin-xtal.mp3",
                        "size": 7340032,
                        "state": "Queued, Remotely",
                        "bytesTransferred": 0,
                        "placeInQueue": 7,
                    }
                ],
            }
        ],
    }
]

#: Older slskd builds returned a flat list rather than the grouped shape.
DOWNLOADS_FLAT = [
    {
        "id": "flat-1",
        "username": "peer_flat",
        "filename": "D:\\music\\track.mp3",
        "size": 5242880,
        "state": "InProgress",
        "bytesTransferred": 2621440,
        "averageSpeed": 131072.0,
    }
]
