"""Secure credential storage.

Passwords, API keys and tokens are kept in the operating system's own
credential store (Windows Credential Manager, macOS Keychain, Secret Service on
Linux) through the ``keyring`` package. They are never written to the settings
file, never written to the database and never written to a log.

If the operating system store is genuinely unavailable, Trackfetch falls back to
an obfuscated file inside its own data folder with restrictive permissions, and
records that fact so the diagnostics report can say so honestly. The fallback is
obfuscation, not encryption, and the code says so rather than pretending
otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from trackfetch.config.paths import app_paths

log = logging.getLogger(__name__)

SERVICE_NAME = "Trackfetch"


class SecretStore:
    """Read and write named secrets without ever exposing them elsewhere."""

    def __init__(self, service: str = SERVICE_NAME, fallback_file: Path | None = None):
        self._service = service
        self._lock = threading.RLock()
        self._fallback_file = fallback_file or (app_paths().data_dir / "credentials.dat")
        self._backend_name = "unavailable"
        self._keyring = None
        self._probe_backend()

    # -- backend selection -------------------------------------------------

    def _probe_backend(self) -> None:
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            if isinstance(backend, FailKeyring):
                raise RuntimeError("no usable keyring backend")
            # A read probe is the only reliable way to know the backend works.
            keyring.get_password(self._service, "__trackfetch_probe__")
            self._keyring = keyring
            self._backend_name = type(backend).__name__
        except Exception as exc:  # pragma: no cover - depends on host OS
            log.info("Operating system credential store unavailable (%s)", exc)
            self._keyring = None
            self._backend_name = "file-fallback"

    @property
    def backend(self) -> str:
        """Name of the backend in use. Safe to include in diagnostics."""
        return self._backend_name

    @property
    def uses_os_store(self) -> bool:
        return self._keyring is not None

    # -- public API --------------------------------------------------------

    @staticmethod
    def key_for(provider_id: str, field_name: str) -> str:
        return f"{provider_id}.{field_name}"

    def get(self, key: str) -> str:
        with self._lock:
            if self._keyring is not None:
                try:
                    return self._keyring.get_password(self._service, key) or ""
                except Exception as exc:  # pragma: no cover - host specific
                    log.warning("Could not read credential %s from the OS store", key)
                    log.debug("credential read error: %s", exc)
                    return ""
            return self._file_read().get(key, "")

    def set(self, key: str, value: str) -> None:
        with self._lock:
            if not value:
                self.delete(key)
                return
            if self._keyring is not None:
                try:
                    self._keyring.set_password(self._service, key, value)
                    return
                except Exception as exc:  # pragma: no cover - host specific
                    log.warning("Could not write credential %s to the OS store", key)
                    log.debug("credential write error: %s", exc)
            data = self._file_read()
            data[key] = value
            self._file_write(data)

    def delete(self, key: str) -> None:
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(self._service, key)
                except Exception:  # pragma: no cover - missing entry is fine
                    pass
            data = self._file_read()
            if key in data:
                del data[key]
                self._file_write(data)

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    # -- obfuscated file fallback -----------------------------------------

    def _cipher_key(self) -> bytes:
        seed = f"{self._service}:{self._fallback_file}".encode("utf-8")
        return hashlib.sha256(seed).digest()

    def _xor(self, data: bytes) -> bytes:
        key = self._cipher_key()
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

    def _file_read(self) -> dict[str, str]:
        try:
            raw = self._fallback_file.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not read the local credential file: %s", exc)
            return {}
        try:
            decoded = self._xor(base64.b64decode(raw))
            parsed = json.loads(decoded.decode("utf-8"))
        except Exception:
            log.warning("Local credential file is unreadable and will be replaced")
            return {}
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}

    def _file_write(self, data: dict[str, str]) -> None:
        payload = base64.b64encode(self._xor(json.dumps(data).encode("utf-8")))
        self._fallback_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._fallback_file.with_suffix(".tmp")
        tmp.write_bytes(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - Windows ignores this
            pass
        tmp.replace(self._fallback_file)


class InMemorySecretStore(SecretStore):
    """Secret store used by the test suite. Touches nothing outside the process."""

    def __init__(self) -> None:  # noqa: D107 - deliberately does not call super
        self._service = SERVICE_NAME
        self._lock = threading.RLock()
        self._fallback_file = Path("/nonexistent")
        self._keyring = None
        self._backend_name = "in-memory"
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str:
        with self._lock:
            return self._data.get(key, "")

    def set(self, key: str, value: str) -> None:
        with self._lock:
            if value:
                self._data[key] = value
            else:
                self._data.pop(key, None)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
