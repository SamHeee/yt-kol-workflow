#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bitable transport backed by an authenticated Feishu/Lark CLI profile.

The high-level table, field, and record operations live in :mod:`bitable`.
This module only replaces the HTTP transport, so CLI user authentication and
application-credential authentication expose the same client interface.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, Optional, Union

from .bitable import BitableClient


Runner = Callable[..., Any]


class LarkCliBitableClient(BitableClient):
    """Run Bitable OpenAPI requests through ``lark-cli api``.

    ``runner`` follows the :func:`subprocess.run` calling convention.  It is
    injectable to keep command construction, error handling, and inherited
    CRUD behaviour fully testable without invoking a real CLI process.
    """

    def __init__(
        self,
        app_token: str,
        *,
        cli_path: Union[str, os.PathLike[str]] = "lark-cli",
        profile: str = "kol-workflow",
        timeout: float = 60,
        runner: Optional[Runner] = None,
    ) -> None:
        if not app_token:
            raise ValueError("app_token cannot be empty")
        if not profile:
            raise ValueError("profile cannot be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        # The inherited CRUD methods do not access ``auth`` because this class
        # overrides every transport primitive.
        super().__init__(app_token=app_token, auth=None)  # type: ignore[arg-type]
        self.cli_path = os.fspath(cli_path)
        self.profile = profile
        self.timeout = timeout
        self._runner = runner or subprocess.run

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data=data)

    def _put(self, path: str, data: dict) -> dict:
        return self._request("PUT", path, data=data)

    def _delete(self, path: str, data: dict = None) -> dict:
        return self._request("DELETE", path, data=data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> dict:
        method = method.upper()
        if not path.startswith("/"):
            raise ValueError("Bitable API path must start with '/'")

        endpoint = f"/open-apis/bitable/v1/apps/{self.app_token}{path}"
        command = [
            self.cli_path,
            "--profile",
            self.profile,
            "api",
            method,
            endpoint,
            "--as",
            "user",
            "--format",
            "json",
        ]

        if method == "GET" and params is not None:
            command.extend(["--params", self._json_dump(params)])

        stdin_data: Optional[str] = None
        if data is not None:
            # Keeping request JSON on stdin prevents record content (including
            # contact details) from appearing in process listings.
            command.extend(["--data", "-"])
            stdin_data = self._json_dump(data)

        try:
            completed = self._runner(
                command,
                input=stdin_data,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"lark-cli {method} request timed out after {self.timeout:g} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"lark-cli executable was not found: {self._sanitize(self.cli_path)}"
            ) from exc
        except OSError as exc:
            detail = self._sanitize(str(exc))
            raise RuntimeError(f"unable to start lark-cli: {detail}") from exc

        stdout = self._to_text(getattr(completed, "stdout", ""))
        stderr = self._to_text(getattr(completed, "stderr", ""))
        return_code = getattr(completed, "returncode", 0)

        if return_code != 0:
            detail = self._error_detail(stdout, stderr)
            raise RuntimeError(
                f"lark-cli {method} request failed (exit={return_code}): {detail}"
            )

        try:
            envelope = self._parse_json(stdout)
        except ValueError as exc:
            detail = self._sanitize(stderr or stdout or "empty output")
            raise RuntimeError(
                f"lark-cli {method} returned invalid JSON: {detail}"
            ) from exc

        return self._normalize_envelope(envelope, method)

    def _normalize_envelope(self, envelope: Any, method: str) -> dict:
        if not isinstance(envelope, dict):
            raise RuntimeError(f"lark-cli {method} returned a non-object JSON response")

        if "ok" in envelope:
            if envelope.get("ok") is not True:
                detail = self._format_cli_error(envelope.get("error"))
                raise RuntimeError(f"lark-cli {method} request was rejected: {detail}")
            payload = envelope.get("data")
        else:
            # Accept raw API JSON as a compatibility fallback for CLI versions
            # that honour --format json without adding the standard envelope.
            payload = envelope

        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            if code not in (0, "0", None):
                message = self._sanitize(str(payload.get("msg") or "OpenAPI error"))
                raise RuntimeError(
                    f"Feishu Bitable API {method} failed (code={code}): {message}"
                )
            return {
                "code": 0,
                "msg": payload.get("msg", "success"),
                "data": payload.get("data") if payload.get("data") is not None else {},
            }

        return {
            "code": 0,
            "msg": "success",
            "data": payload if payload is not None else {},
        }

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _parse_json(text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty JSON output")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            # Some CLI versions emit a short informational line before the
            # machine-readable response.  Decode the first valid JSON value
            # without ever echoing the raw output to logs.
            decoder = json.JSONDecoder()
            for index, character in enumerate(stripped):
                if character not in "{[":
                    continue
                try:
                    value, _ = decoder.raw_decode(stripped[index:])
                except json.JSONDecodeError:
                    continue
                return value
            raise ValueError("no JSON value found")

    def _error_detail(self, stdout: str, stderr: str) -> str:
        for candidate in (stdout, stderr):
            try:
                value = self._parse_json(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                if "error" in value:
                    return self._format_cli_error(value.get("error"))
                if value.get("msg"):
                    return self._sanitize(str(value["msg"]))
        return self._sanitize(stderr or stdout or "no diagnostic output")

    def _format_cli_error(self, error: Any) -> str:
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("msg") or "CLI error"
            detail = self._sanitize(str(message))
            return f"{detail} (code={self._sanitize(str(code))})" if code else detail
        return self._sanitize(str(error or "unknown CLI error"))

    def _sanitize(self, value: str) -> str:
        """Remove credentials and identifiers from exception diagnostics."""
        safe = value.replace(self.app_token, "<redacted-app-token>")
        secret_names = (
            r"device[_-]?code|access[_-]?token|refresh[_-]?token|"
            r"tenant[_-]?access[_-]?token|app[_-]?secret|authorization"
        )
        safe = re.sub(
            rf"(?i)([\"']?(?:{secret_names})[\"']?\s*[:=]\s*[\"']?)"
            r"[^\s\"',}\]]+",
            r"\1<redacted>",
            safe,
        )
        safe = re.sub(r"(?i)\bBearer\s+[^\s\"',}\]]+", "Bearer <redacted>", safe)
        safe = " ".join(safe.split())
        return safe[:500] if safe else "no diagnostic output"
