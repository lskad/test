"""Store the DeepSeek credential using current-user Windows Data Protection."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _credential_path() -> Path:
    return Path.home() / ".agent-month-lab" / "deepseek.dpapi"


def _transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("本地密钥存储需要 Windows DPAPI。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    blob_ptr = ctypes.POINTER(_Blob)
    description = wintypes.LPCWSTR if protect else ctypes.POINTER(wintypes.LPWSTR)
    operation.argtypes = [
        blob_ptr, description, blob_ptr, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, blob_ptr,
    ]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    output = _Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN. Omitting LOCAL_MACHINE binds to this user.
        if not operation(ctypes.byref(source), None, None, None, None, 1,
                         ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


def load_key() -> str:
    """Return the stored key, or an empty string when no file exists."""
    try:
        ciphertext = _credential_path().read_bytes()
    except FileNotFoundError:
        return ""
    return _transform(ciphertext, protect=False).decode("utf-8")


def save_key(key: str) -> None:
    """Atomically replace the credential after successful encryption."""
    key = key.strip()
    if not key:
        raise ValueError("API Key 不能为空。")
    ciphertext = _transform(key.encode("utf-8"), protect=True)
    target = _credential_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(dir=target.parent, delete=False)
    try:
        with temporary:
            temporary.write(ciphertext)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, target)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
