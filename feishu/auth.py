#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu API authentication and tenant access-token management.

Tenant access tokens are cached in memory on every platform.  On POSIX, a
small on-disk cache is also used after its owner and permissions have been
validated.  Windows has no secure file-permission primitive in the standard
library that is equivalent to POSIX ``0600``, so plaintext disk caching is
disabled there by default.  It can be explicitly enabled with
``FEISHU_ALLOW_INSECURE_TOKEN_CACHE=1`` for backwards compatibility, but that
setting is not recommended.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("kol_workflow.feishu.auth")

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

_CACHE_DIR = Path.home() / ".kol_workflow"
_WINDOWS_INSECURE_CACHE_ENV = "FEISHU_ALLOW_INSECURE_TOKEN_CACHE"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _is_windows() -> bool:
    return os.name == "nt"


def _disk_cache_enabled() -> bool:
    """Return whether plaintext tokens may be persisted on this platform."""
    if not _is_windows():
        return True
    return os.environ.get(_WINDOWS_INSECURE_CACHE_ENV, "").strip().lower() in _TRUE_VALUES


def _current_uid() -> Optional[int]:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _ensure_cache_dir() -> bool:
    """Create and restrict the cache directory without making auth fail.

    On POSIX, failure to prove that the directory belongs to the current user
    and is mode ``0700`` disables disk caching for that operation.  Windows
    reaches this function only after the user explicitly opted into plaintext
    caching, so no unsupported ACL guarantees are made.
    """
    try:
        _CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _is_windows():
            return True

        directory_stat = _CACHE_DIR.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            logger.warning("Token cache directory is not a real directory; disk cache disabled")
            return False

        current_uid = _current_uid()
        if current_uid is not None and directory_stat.st_uid != current_uid:
            logger.warning("Token cache directory has an unsafe owner; disk cache disabled")
            return False

        os.chmod(_CACHE_DIR, 0o700)
        if stat.S_IMODE(_CACHE_DIR.stat().st_mode) != 0o700:
            logger.warning("Token cache directory permissions are unsafe; disk cache disabled")
            return False
        return True
    except OSError as exc:
        logger.warning(
            "Token cache directory could not be secured; disk cache disabled (%s)",
            type(exc).__name__,
        )
        return False


def _cache_file_is_secure(file_stat: os.stat_result) -> bool:
    """Validate a POSIX cache descriptor before reading its contents."""
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    current_uid = _current_uid()
    if current_uid is not None and file_stat.st_uid != current_uid:
        return False
    mode = stat.S_IMODE(file_stat.st_mode)
    return mode == 0o600


class FeishuAuth:
    """Manage Feishu tenant access tokens with automatic refresh.

    Args:
        app_id: Feishu app ID. If empty, read ``FEISHU_APP_ID``.
        app_secret: Feishu app secret. If empty, read ``FEISHU_APP_SECRET``.
    """

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self._resolve_credentials(app_id, app_secret)
        self._token: Optional[str] = None
        self._expires_at: float = 0
        self._disk_cache = _disk_cache_enabled()
        if _is_windows() and self._disk_cache:
            logger.warning(
                "Plaintext token disk caching was explicitly enabled on Windows; "
                "this is not recommended"
            )

    def _resolve_credentials(self, app_id: str, app_secret: str):
        """Resolve credentials from arguments or environment variables."""
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")

    def _token_cache_path(self) -> Path:
        """Return the legacy-compatible cache path without creating it."""
        safe_id = self.app_id.replace("/", "_").replace("\\", "_")
        return _CACHE_DIR / f".feishu_token_{safe_id}"

    def _load_cached_token(self) -> Tuple[Optional[str], float]:
        """Load a validly-formed token cache, or return an empty result."""
        if not self._disk_cache or not _ensure_cache_dir():
            return None, 0

        cache_file = self._token_cache_path()
        try:
            if _is_windows():
                # This path is reachable only through the explicit insecure opt-in.
                data = json.loads(cache_file.read_text(encoding="utf-8"))
            else:
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(cache_file, flags)
                try:
                    if not _cache_file_is_secure(os.fstat(descriptor)):
                        logger.warning("Unsafe token cache file ignored")
                        return None, 0
                    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                        descriptor = -1
                        data = json.load(stream)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)

            token = data.get("token", "") if isinstance(data, dict) else ""
            expires_at = data.get("expires_at", 0) if isinstance(data, dict) else 0
            if isinstance(token, str) and token and isinstance(expires_at, (int, float)):
                if expires_at > 0:
                    return token, float(expires_at)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.debug("Token cache could not be loaded (%s)", type(exc).__name__)
        return None, 0

    def _save_token_cache(self, token: str, expires_at: float):
        """Atomically persist a token when platform policy allows it."""
        if not self._disk_cache or not _ensure_cache_dir():
            return

        cache_file = self._token_cache_path()
        temp_path: Optional[Path] = None
        descriptor = -1
        try:
            descriptor, raw_temp_path = tempfile.mkstemp(
                prefix=f"{cache_file.name}.",
                suffix=".tmp",
                dir=str(_CACHE_DIR),
            )
            temp_path = Path(raw_temp_path)
            if not _is_windows():
                os.fchmod(descriptor, 0o600)

            payload = json.dumps(
                {"token": token, "expires_at": expires_at},
                ensure_ascii=False,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temp_path, cache_file)
            temp_path = None
            if not _is_windows():
                os.chmod(cache_file, 0o600)
        except OSError as exc:
            logger.warning("Token could not be cached (%s)", type(exc).__name__)
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _clear_token_cache(self):
        """Remove a legacy disk cache, even when new writes are disabled."""
        try:
            self._token_cache_path().unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Token cache could not be cleared (%s)", type(exc).__name__)

    def get_token(self) -> str:
        """Return a valid tenant access token, refreshing when necessary."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        cached_token, cached_expires_at = self._load_cached_token()
        if cached_token and time.time() < cached_expires_at - 60:
            self._token = cached_token
            self._expires_at = cached_expires_at
            logger.debug("Using token from the local cache")
            return self._token

        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        data = {"app_id": self.app_id, "app_secret": self.app_secret}

        try:
            response = requests.post(url, json=data, timeout=30)
            result = response.json()
        except Exception as exc:
            raise RuntimeError(f"飞书认证请求失败: {exc}") from exc

        if result.get("code") != 0:
            raise RuntimeError(
                f"飞书认证失败: {result.get('msg', 'Unknown error')} "
                f"(code={result.get('code')})"
            )

        self._token = result["tenant_access_token"]
        self._expires_at = time.time() + result.get("expire", 7200)
        self._save_token_cache(self._token, self._expires_at)

        logger.info("飞书认证成功")
        return self._token

    def get_headers(self) -> dict:
        """Return HTTP headers containing a valid bearer token."""
        token = self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def invalidate(self):
        """Clear memory and disk caches, forcing authentication next time."""
        self._token = None
        self._expires_at = 0
        self._clear_token_cache()
        logger.info("飞书认证已失效，下次调用将重新认证")
