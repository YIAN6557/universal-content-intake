"""Local secrets (Queue API key, translation API keys): the macOS Keychain, or Windows Credential Manager.

Values are never printed, logged or written to files. On macOS ``security -i``
reads the write command from stdin, so a value never appears in a process
listing; on Windows the Credential Manager API is called directly.
"""

from __future__ import annotations

import subprocess
import sys
import threading

from src.core import compat


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
        if self._cached_secret is not None:
            return self._cached_secret
        if compat.WINDOWS:
            secret = read_secret(self.service, self.account)
            if not secret:
                raise SecretProviderError("Credential Manager item is unavailable.")
            self._cached_secret = secret
            return secret
        if sys.platform != "darwin":
            raise SecretProviderError("Queue API Keychain credentials are available only on macOS and Windows.")
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


# --- generic store ---------------------------------------------------------

def read_secret(service: str, account: str) -> str | None:
    """The stored value, or None when there is none (or the store cannot be read)."""

    if compat.WINDOWS:
        return _windows_read(f"{service}:{account}")
    try:
        return KeychainSecretProvider(service=service, account=account).get_secret()
    except SecretProviderError:
        return None


def write_secret(service: str, account: str, value: str) -> None:
    if not value or "\n" in value or '"' in value:
        raise SecretProviderError("The value is empty or contains characters a key never has.")
    if compat.WINDOWS:
        _windows_write(f"{service}:{account}", value)
        return
    command = f'add-generic-password -U -s "{service}" -a "{account}" -w "{value}"\n'
    result = subprocess.run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0 or read_secret(service, account) != value:
        raise SecretProviderError("Could not write to the macOS Keychain; make sure it is unlocked.")


def delete_secret(service: str, account: str) -> None:
    if compat.WINDOWS:
        _windows_delete(f"{service}:{account}")
        return
    subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, timeout=20, check=False)


# Windows Credential Manager (advapi32), generic credentials stored for the current user.
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _advapi():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    library = ctypes.WinDLL("advapi32", use_last_error=True)
    library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    library.CredReadW.restype = wintypes.BOOL
    library.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, library, CREDENTIAL


def _windows_read(target: str) -> str | None:
    ctypes, library, CREDENTIAL = _advapi()
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not library.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
    finally:
        library.CredFree(pointer)
    return blob.decode("utf-8") or None


def _windows_write(target: str, value: str) -> None:
    ctypes, library, CREDENTIAL = _advapi()
    blob = value.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIAL(Type=_CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                            Persist=_CRED_PERSIST_LOCAL_MACHINE, UserName="Universal Content Intake")
    if not library.CredWriteW(ctypes.byref(credential), 0):
        raise SecretProviderError(f"Could not write to Windows Credential Manager (error {ctypes.get_last_error()}).")


def _windows_delete(target: str) -> None:
    _, library, _ = _advapi()
    library.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
