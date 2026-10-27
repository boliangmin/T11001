#!/usr/bin/env python3
"""使用 Google Drive appDataFolder 上传和下载完整应用状态。"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
CLOUD_FILENAME = "trade-view-state-v1.json"
CLOUD_SCHEMA_VERSION = 1
OAUTH_CONFIG_FILENAME = "google_oauth_client.json"
TOKEN_FILENAME = "google_oauth_token.bin"
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_ROOT = "https://www.googleapis.com/upload/drive/v3"


class CloudSyncError(RuntimeError):
    """Google 授权、Drive 请求或云端快照无效。"""

    def __init__(self, message: str, *, oauth_error: str = "") -> None:
        super().__init__(message)
        # Google OAuth 错误代码（如 invalid_grant），用于判断是否需要重新授权。
        self.oauth_error = oauth_error


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str
    auth_uri: str
    token_uri: str
    source_path: Path


@dataclass(frozen=True)
class CloudSnapshotInfo:
    revision: int
    uploaded_at: str
    source_device: str
    state_sha256: str
    drive_file_id: str = ""


@dataclass(frozen=True)
class _DriveFile:
    file_id: str
    modified_time: str
    version: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _windows_data_protection_api() -> tuple[object, object]:
    blob_pointer = ctypes.POINTER(_DataBlob)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        blob_pointer,
        wintypes.LPCWSTR,
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_pointer,
        ctypes.POINTER(wintypes.LPWSTR),
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _local_app_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "StockPriceViewer"
    return Path.home() / ".stock_price_viewer"


def _application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def oauth_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("TRADE_VIEW_GOOGLE_OAUTH_CLIENT_FILE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            _local_app_data_dir() / OAUTH_CONFIG_FILENAME,
            _application_dir() / OAUTH_CONFIG_FILENAME,
        ]
    )
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _chrome_executable() -> Path:
    candidates: list[Path] = []
    discovered = shutil.which("chrome.exe") or shutil.which("chrome")
    if discovered:
        candidates.append(Path(discovered))
    for environment_name in (
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LOCALAPPDATA",
    ):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CloudSyncError(
        "未找到 Google Chrome。请先安装 Chrome，再重新执行云端操作。"
    )


def _open_authorization_in_chrome(authorization_url: str) -> None:
    chrome = _chrome_executable()
    try:
        subprocess.Popen(
            [str(chrome), "--new-window", authorization_url],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise CloudSyncError(f"无法启动 Google Chrome：{exc}") from exc


def _load_oauth_config() -> OAuthClientConfig:
    candidates = oauth_config_candidates()
    config_path = next((path for path in candidates if path.is_file()), None)
    if config_path is None:
        locations = "\n".join(f"- {path}" for path in candidates)
        raise CloudSyncError(
            "尚未配置 Google OAuth 客户端。请从 Google Cloud 下载桌面应用"
            f"客户端 JSON，并保存为：\n{locations}"
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        installed = payload.get("installed")
        if not isinstance(installed, dict):
            raise ValueError("必须是 Desktop app 类型的 installed 配置")
        client_id = str(installed.get("client_id") or "").strip()
        client_secret = str(installed.get("client_secret") or "").strip()
        auth_uri = str(installed.get("auth_uri") or DEFAULT_AUTH_URI).strip()
        token_uri = str(installed.get("token_uri") or DEFAULT_TOKEN_URI).strip()
        if not client_id:
            raise ValueError("缺少 client_id")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CloudSyncError(f"Google OAuth 客户端配置无效：{exc}") from exc
    return OAuthClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        auth_uri=auth_uri,
        token_uri=token_uri,
        source_path=config_path,
    )


def _protect_for_current_windows_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise CloudSyncError("当前版本仅支持在 Windows 中保存 Google 授权信息")
    buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _DataBlob()
    crypt32, kernel32 = _windows_data_protection_api()
    ctypes.set_last_error(0)
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "TradeView Google OAuth",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise CloudSyncError(f"无法加密 Google 授权信息：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(
                ctypes.cast(output_blob.pbData, ctypes.c_void_p)
            )


def _unprotect_for_current_windows_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise CloudSyncError("当前版本仅支持在 Windows 中读取 Google 授权信息")
    buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _DataBlob()
    crypt32, kernel32 = _windows_data_protection_api()
    ctypes.set_last_error(0)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise CloudSyncError(f"无法读取 Google 授权信息：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(
                ctypes.cast(output_blob.pbData, ctypes.c_void_p)
            )


def _token_path() -> Path:
    return _local_app_data_dir() / TOKEN_FILENAME


def _load_token() -> dict[str, object] | None:
    path = _token_path()
    if not path.is_file():
        return None
    try:
        decrypted = _unprotect_for_current_windows_user(path.read_bytes())
        payload = json.loads(decrypted.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, CloudSyncError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_token(payload: dict[str, object]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    protected = _protect_for_current_windows_user(encoded)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(protected)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _http_error_details(exc: HTTPError) -> tuple[str, str]:
    """返回可读的错误说明和 Google OAuth 错误代码（没有时为空字符串）。"""
    try:
        body = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        if isinstance(payload, dict):
            error = payload.get("error")
            oauth_error = error if isinstance(error, str) else ""
            detail = payload.get("error_description") or error
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status")
            if detail:
                return f"HTTP {exc.code}: {detail}", oauth_error
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return f"HTTP {exc.code}: {exc.reason}", ""


def _http_error_message(exc: HTTPError) -> str:
    return _http_error_details(exc)[0]


def _post_form(url: str, values: dict[str, str], timeout: float) -> dict[str, object]:
    data = urlencode(values).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message, oauth_error = _http_error_details(exc)
        raise CloudSyncError(message, oauth_error=oauth_error) from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloudSyncError(f"Google 授权请求失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise CloudSyncError("Google 授权返回的数据格式无效")
    return payload


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "_OAuthCallbackServer"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        query = parse_qs(urlparse(self.path).query)
        self.server.authorization_result = {
            key: values[0] for key, values in query.items() if values
        }
        body = (
            "<!doctype html><meta charset='utf-8'><title>授权完成</title>"
            "<style>body{font-family:sans-serif;padding:40px}</style>"
            "<h2>Google Drive 授权已完成</h2><p>可以关闭此页面并返回“我钱呢”。</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _OAuthCallbackServer(HTTPServer):
    authorization_result: dict[str, str] | None = None


def _authorize_interactively(
    config: OAuthClientConfig,
    timeout: float,
    status_callback: Callable[[str], None] | None,
) -> dict[str, object]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    server = _OAuthCallbackServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    authorization_params = {
        'client_id': config.client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': DRIVE_SCOPE,
        'access_type': 'offline',
        'prompt': 'select_account consent',
        'include_granted_scopes': 'true',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    authorization_url = f"{config.auth_uri}?{urlencode(authorization_params)}"
    if status_callback is not None:
        status_callback("请在 Google Chrome 中选择账户并完成授权……")
    _open_authorization_in_chrome(authorization_url)
    deadline = time.monotonic() + timeout
    try:
        while server.authorization_result is None and time.monotonic() < deadline:
            server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    result = server.authorization_result
    if result is None:
        raise CloudSyncError("等待 Google 授权超时，请重新操作")
    if result.get("state") != state:
        raise CloudSyncError("Google 授权状态校验失败")
    if result.get("error"):
        raise CloudSyncError(f"Google 授权失败：{result['error']}")
    code = result.get("code", "")
    if not code:
        raise CloudSyncError("Google 未返回授权代码")

    values = {
        "client_id": config.client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if config.client_secret:
        values["client_secret"] = config.client_secret
    token = _post_form(config.token_uri, values, timeout=30.0)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise CloudSyncError("Google 未返回访问令牌")
    token["client_id"] = config.client_id
    token["expires_at"] = time.time() + float(token.get("expires_in") or 3600)
    _save_token(token)
    return token


def _refresh_access_token(
    config: OAuthClientConfig,
    token: dict[str, object],
) -> dict[str, object]:
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        raise CloudSyncError(
            "Google 授权已过期且没有刷新令牌，请重新授权",
            oauth_error="invalid_grant",
        )
    values = {
        "client_id": config.client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if config.client_secret:
        values["client_secret"] = config.client_secret
    refreshed = _post_form(config.token_uri, values, timeout=30.0)
    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise CloudSyncError("Google 未返回新的访问令牌")
    refreshed["refresh_token"] = refresh_token
    refreshed["client_id"] = config.client_id
    refreshed["expires_at"] = time.time() + float(refreshed.get("expires_in") or 3600)
    _save_token(refreshed)
    return refreshed


def _canonical_state_bytes(state: dict[str, object]) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_state_payload(state: object) -> dict[str, object]:
    if not isinstance(state, dict):
        raise CloudSyncError("云端状态根节点不是对象")
    required_types = {
        "version": int,
        "saved_at": str,
        "window_geometry": str,
        "column_widths": dict,
        "trade_window_geometry": str,
        "trade_column_widths": dict,
        "active_market": str,
        "active_page": str,
        "show_closed_positions": dict,
        "stocks": list,
        "cash_flows": list,
        "trades": dict,
        "t_trade_groups": dict,
    }
    for key, expected_type in required_types.items():
        if not isinstance(state.get(key), expected_type):
            raise CloudSyncError(f"云端状态字段 {key} 格式无效")
    if state["version"] not in {1, 2, 3, 4}:
        raise CloudSyncError("云端状态版本不受支持")
    return state


def build_cloud_snapshot(
    state_bytes: bytes,
    revision: int,
    *,
    source_device: str = "windows-desktop",
) -> tuple[bytes, CloudSnapshotInfo]:
    try:
        state = json.loads(state_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CloudSyncError(f"本地状态文件格式无效：{exc}") from exc
    validated = validate_state_payload(state)
    state_sha256 = hashlib.sha256(_canonical_state_bytes(validated)).hexdigest().upper()
    uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot = {
        "cloud_schema_version": CLOUD_SCHEMA_VERSION,
        "revision": revision,
        "uploaded_at": uploaded_at,
        "source_device": source_device,
        "state_sha256": state_sha256,
        "state": validated,
    }
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    return payload, CloudSnapshotInfo(
        revision=revision,
        uploaded_at=uploaded_at,
        source_device=source_device,
        state_sha256=state_sha256,
    )


def extract_cloud_snapshot(payload: bytes) -> tuple[bytes, CloudSnapshotInfo]:
    try:
        snapshot = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CloudSyncError(f"云端快照格式无效：{exc}") from exc
    if not isinstance(snapshot, dict):
        raise CloudSyncError("云端快照根节点不是对象")
    if snapshot.get("cloud_schema_version") != CLOUD_SCHEMA_VERSION:
        raise CloudSyncError("云端快照版本不受支持")
    revision = snapshot.get("revision")
    uploaded_at = snapshot.get("uploaded_at")
    source_device = snapshot.get("source_device")
    expected_sha256 = snapshot.get("state_sha256")
    if not isinstance(revision, int) or revision < 1:
        raise CloudSyncError("云端快照 revision 无效")
    if not all(isinstance(value, str) and value for value in (
        uploaded_at,
        source_device,
        expected_sha256,
    )):
        raise CloudSyncError("云端快照元数据不完整")
    state = validate_state_payload(snapshot.get("state"))
    actual_sha256 = hashlib.sha256(_canonical_state_bytes(state)).hexdigest().upper()
    if actual_sha256 != expected_sha256.upper():
        raise CloudSyncError("云端快照 SHA-256 校验失败")
    state_bytes = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    return state_bytes, CloudSnapshotInfo(
        revision=revision,
        uploaded_at=uploaded_at,
        source_device=source_device,
        state_sha256=actual_sha256,
    )


class GoogleDriveSyncClient:
    def __init__(
        self,
        status_callback: Callable[[str], None] | None = None,
        *,
        force_interactive_authorization: bool = False,
        authorize_if_needed: bool = False,
    ) -> None:
        self._status_callback = status_callback
        self._config = _load_oauth_config()
        self._access_token = self._get_access_token(
            force_interactive_authorization=force_interactive_authorization,
            authorize_if_needed=authorize_if_needed,
        )

    def _get_access_token(
        self,
        *,
        force_interactive_authorization: bool,
        authorize_if_needed: bool,
    ) -> str:
        if force_interactive_authorization:
            token = _authorize_interactively(
                self._config,
                timeout=300.0,
                status_callback=self._status_callback,
            )
            return str(token["access_token"])

        token = _load_token()
        if token is not None and token.get("client_id") == self._config.client_id:
            access_token = str(token.get("access_token") or "")
            expires_at = float(token.get("expires_at") or 0)
            if access_token and expires_at > time.time() + 90:
                return access_token
            try:
                refreshed = _refresh_access_token(self._config, token)
                return str(refreshed["access_token"])
            except CloudSyncError as exc:
                # 刷新令牌过期、被撤销或缺失时重新授权，其他错误照常报告。
                if exc.oauth_error != "invalid_grant":
                    raise
        if authorize_if_needed:
            token = _authorize_interactively(
                self._config,
                timeout=300.0,
                status_callback=self._status_callback,
            )
            return str(token["access_token"])
        raise CloudSyncError(
            "尚未登录 Google。"
        )

    def verify_access(self) -> None:
        if self._status_callback is not None:
            self._status_callback("正在确认 Google Drive 授权……")
        self._find_cloud_file()

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[bytes, dict[str, str]]:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read(), dict(response.headers.items())
        except HTTPError as exc:
            raise CloudSyncError(f"Google Drive 请求失败：{_http_error_message(exc)}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CloudSyncError(f"Google Drive 网络请求失败：{exc}") from exc

    def _find_cloud_file(self) -> _DriveFile | None:
        params = urlencode(
            {
                "spaces": "appDataFolder",
                "q": (
                    f"name = '{CLOUD_FILENAME}' "
                    "and 'appDataFolder' in parents"
                ),
                "orderBy": "modifiedTime desc",
                "pageSize": 10,
                "fields": "files(id,modifiedTime,version)",
            }
        )
        raw, _headers = self._request(f"{DRIVE_API_ROOT}/files?{params}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            files = payload.get("files") or []
        except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            raise CloudSyncError("Google Drive 文件列表格式无效") from exc
        if not files:
            return None
        item = files[0]
        return _DriveFile(
            file_id=str(item.get("id") or ""),
            modified_time=str(item.get("modifiedTime") or ""),
            version=str(item.get("version") or ""),
        )

    def _download_file(self, file_id: str) -> bytes:
        raw, _headers = self._request(
            f"{DRIVE_API_ROOT}/files/{file_id}?alt=media",
            timeout=60.0,
        )
        return raw

    def upload_state(self, state_bytes: bytes) -> CloudSnapshotInfo:
        if self._status_callback is not None:
            self._status_callback("正在读取 Google Drive 云端版本……")
        existing = self._find_cloud_file()
        revision = 1
        if existing is not None:
            try:
                _state, current_info = extract_cloud_snapshot(
                    self._download_file(existing.file_id)
                )
                revision = current_info.revision + 1
            except CloudSyncError:
                revision = 1
        payload, info = build_cloud_snapshot(state_bytes, revision)
        if self._status_callback is not None:
            self._status_callback("正在上传完整状态到 Google Drive……")
        if existing is None:
            boundary = f"trade-view-{secrets.token_hex(16)}"
            metadata = json.dumps(
                {
                    "name": CLOUD_FILENAME,
                    "parents": ["appDataFolder"],
                    "mimeType": "application/json",
                }
            ).encode("utf-8")
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            ).encode("utf-8") + metadata + (
                f"\r\n--{boundary}\r\nContent-Type: application/json; charset=UTF-8"
                f"\r\n\r\n"
            ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
            raw, _headers = self._request(
                f"{DRIVE_UPLOAD_ROOT}/files?uploadType=multipart&fields=id",
                method="POST",
                data=body,
                content_type=f"multipart/related; boundary={boundary}",
                timeout=60.0,
            )
        else:
            raw, _headers = self._request(
                f"{DRIVE_UPLOAD_ROOT}/files/{existing.file_id}?uploadType=media&fields=id",
                method="PATCH",
                data=payload,
                content_type="application/json; charset=UTF-8",
                timeout=60.0,
            )
        try:
            response = json.loads(raw.decode("utf-8"))
            file_id = str(response.get("id") or "")
        except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            raise CloudSyncError("Google Drive 上传结果格式无效") from exc
        if not file_id:
            raise CloudSyncError("Google Drive 上传后未返回文件 ID")
        return CloudSnapshotInfo(
            revision=info.revision,
            uploaded_at=info.uploaded_at,
            source_device=info.source_device,
            state_sha256=info.state_sha256,
            drive_file_id=file_id,
        )

    def download_state(self) -> tuple[bytes, CloudSnapshotInfo]:
        if self._status_callback is not None:
            self._status_callback("正在查找 Google Drive 云端快照……")
        cloud_file = self._find_cloud_file()
        if cloud_file is None:
            raise CloudSyncError("Google Drive 中还没有可同步的云端快照")
        if self._status_callback is not None:
            self._status_callback("正在下载并校验 Google Drive 云端快照……")
        state_bytes, info = extract_cloud_snapshot(
            self._download_file(cloud_file.file_id)
        )
        return state_bytes, CloudSnapshotInfo(
            revision=info.revision,
            uploaded_at=info.uploaded_at,
            source_device=info.source_device,
            state_sha256=info.state_sha256,
            drive_file_id=cloud_file.file_id,
        )
