"""Local Queue API secret retrieval from the macOS Keychain."""

from __future__ import annotations

import subprocess
import sys
import threading


class SecretProviderError(RuntimeError):
    """A Keychain credential is absent or could not be read safely."""


class KeychainSecretProvider:
    """Read one generic-password item without printing or persisting its value."""

    def __init__(
        self,
        *,
        service: str = "UCI Queue API HMAC",
        account: str = "queue-api",
        timeout_seconds: float = 10,
    ) -> None:
        self.service = service
        self.account = account
        self.timeout_seconds = timeout_seconds
        self._cached_secret: str | None = None
        self._cache_lock = threading.Lock()

    def get_secret(self) -> str:
        if sys.platform != "darwin":
            raise SecretProviderError("Queue API Keychain credentials are available only on macOS.")
        if self._cached_secret is not None:
            return self._cached_secret
        with self._cache_lock:
            if self._cached_secret is not None:
                return self._cached_secret
            command = [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                self.account,
                "-w",
            ]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise SecretProviderError("Queue API Keychain credential is unavailable.") from None
            if result.returncode != 0:
                raise SecretProviderError("Queue API Keychain credential is unavailable.")
            secret = result.stdout.rstrip("\r\n")
            if not secret:
                raise SecretProviderError("Queue API Keychain credential is empty.")
            self._cached_secret = secret
            return secret
