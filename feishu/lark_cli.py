"""Bootstrap and use the official Feishu/Lark CLI authorization flow.

This module deliberately does not expose or copy application secrets.  The CLI
owns its configuration and tokens; the workflow only starts the documented
browser-based setup and verifies that the resulting user authorization works.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


DEFAULT_PROFILE = "kol-workflow"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_BASE_TIME_ZONE = "Asia/Shanghai"

# Only browser URLs owned by Feishu/Lark may be opened automatically.  Keep
# this list deliberately narrow: accepting arbitrary HTTPS URLs would turn CLI
# output into an unsafe browser-launch primitive.
OFFICIAL_BROWSER_DOMAINS = (
    "feishu.cn",
    "larksuite.com",
    "larkoffice.com",
)

_HTTPS_URL_RE = re.compile(r"https://[^\s<>\"'`\[\]{}]+", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_TRAILING_PUNCTUATION = ").,;:!?\uff09\u3002\uff0c\uff1b\uff1a\uff01\uff1f"


class LarkCliError(RuntimeError):
    """Base error for CLI setup and Base creation failures."""


class LarkCliRuntimeError(LarkCliError):
    """Raised when Node.js/npx or lark-cli is unavailable."""


class LarkCliCommandError(LarkCliError):
    """Raised when an external CLI command fails."""


class LarkCliTimeoutError(LarkCliError):
    """Raised when a CLI setup or authorization command times out."""


class UnsafeAuthorizationUrlError(LarkCliError):
    """Raised when CLI output contains a non-official browser URL."""


class BrowserLaunchError(LarkCliError):
    """Raised when the authorization URL cannot be opened automatically."""


@dataclass(frozen=True)
class RuntimeStatus:
    """Resolved executable paths in the current process environment."""

    node: str | None
    npx: str | None
    lark_cli: str | None


@dataclass(frozen=True)
class SetupResult:
    """Summary returned after the named profile is ready for Base requests."""

    profile: str
    cli_path: str
    installed: bool
    configured: bool
    authorized: bool


RunCallable = Callable[..., subprocess.CompletedProcess[str]]
PopenCallable = Callable[..., subprocess.Popen[str]]
WhichCallable = Callable[[str], str | None]
BrowserCallable = Callable[[str], bool | None]
ProgressCallable = Callable[[str], None]


class LarkCliManager:
    """Manage lark-cli installation, profile setup, and Base authorization.

    Parameters are intentionally injectable so tests and embedding applications
    do not need to start subprocesses or a real browser.
    """

    def __init__(
        self,
        profile: str = DEFAULT_PROFILE,
        cli_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        open_browser: bool | BrowserCallable = True,
        auto_install: bool = True,
        *,
        runner: RunCallable | None = None,
        popen_factory: PopenCallable | None = None,
        which: WhichCallable | None = None,
        browser_opener: BrowserCallable | None = None,
        progress: ProgressCallable | None = None,
        monotonic: Callable[[], float] | None = None,
        path_exists: Callable[[str], bool] | None = None,
    ) -> None:
        clean_profile = str(profile or "").strip()
        if not clean_profile:
            raise ValueError("profile must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self.profile = clean_profile
        self._requested_cli_path = cli_path
        self.timeout = float(timeout)
        self.auto_install = bool(auto_install)
        self._runner = runner or subprocess.run
        self._popen_factory = popen_factory or subprocess.Popen
        self._which = which or shutil.which
        self._progress = progress
        self._monotonic = monotonic or time.monotonic
        self._path_exists = path_exists or os.path.isfile

        if callable(open_browser):
            self._open_browser_enabled = True
            self._browser_opener = open_browser
        else:
            self._open_browser_enabled = bool(open_browser)
            self._browser_opener = browser_opener or webbrowser.open_new_tab

        self._cli_path: str | None = None

    @property
    def cli_path(self) -> str | None:
        """Resolved CLI executable, if it is already available."""

        return self._cli_path or self._resolve_cli_path()

    def detect_runtime(self) -> RuntimeStatus:
        """Return resolved Node.js, npx, and CLI paths without changing state."""

        return RuntimeStatus(
            node=self._which("node"),
            npx=self._which("npx"),
            lark_cli=self._resolve_cli_path(),
        )

    def ensure_cli(self) -> tuple[str, bool]:
        """Return a usable CLI path, installing it through npx when needed."""

        existing = self._resolve_cli_path()
        if existing:
            self._cli_path = existing
            return existing, False

        if not self.auto_install:
            raise LarkCliRuntimeError(
                "lark-cli is not installed and automatic installation is disabled"
            )

        node = self._which("node")
        npx = self._which("npx")
        missing = [name for name, path in (("Node.js", node), ("npx", npx)) if not path]
        if missing:
            raise LarkCliRuntimeError(
                "Cannot install lark-cli because the required runtime is missing: "
                + ", ".join(missing)
            )

        self._emit("正在安装飞书 CLI…")
        self._run(
            [str(npx), "--yes", "@larksuite/cli@latest", "install"],
            label="install lark-cli",
            timeout=min(max(self.timeout, 120.0), 600.0),
        )

        installed = self._resolve_cli_path(refresh=True)
        if not installed:
            installed = self._resolve_cli_from_npm_prefix()
        if not installed:
            raise LarkCliRuntimeError(
                "lark-cli installation completed but its executable was not found"
            )

        self._cli_path = installed
        self._emit("飞书 CLI 已安装。")
        return installed, True

    def profile_configured(self) -> bool:
        """Return whether the named CLI profile has an application config."""

        cli = self._require_cli_path()
        result = self._run(
            [cli, "--profile", self.profile, "config", "show"],
            label="check CLI profile",
            timeout=min(self.timeout, 60.0),
            check=False,
        )
        return self._command_indicates_success(result)

    def authorization_valid(self) -> bool:
        """Verify the profile's user token against the Feishu server."""

        cli = self._require_cli_path()
        result = self._run(
            [cli, "--profile", self.profile, "auth", "status", "--verify"],
            label="verify CLI authorization",
            timeout=min(self.timeout, 60.0),
            check=False,
        )
        return self._command_indicates_success(result)

    def ensure_ready(self) -> SetupResult:
        """Install, configure, authorize, and verify the named CLI profile."""

        cli, installed = self.ensure_cli()
        configured_now = False

        if not self.profile_configured():
            self._configure_profile(cli)
            configured_now = True

        if not self.authorization_valid():
            self._authorize(cli)

        if not self.authorization_valid():
            raise LarkCliCommandError(
                "Feishu authorization finished but token verification failed"
            )

        self._emit("飞书授权已验证。")
        return SetupResult(
            profile=self.profile,
            cli_path=cli,
            installed=installed,
            configured=configured_now,
            authorized=True,
        )

    def create_base(
        self,
        name: str,
        *,
        time_zone: str = DEFAULT_BASE_TIME_ZONE,
    ) -> dict[str, str]:
        """Create a Base with CLI user authorization.

        The returned mapping has a stable shape::

            {"name": "...", "app_token": "...", "base_url": "https://..."}
        """

        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Base name must not be empty")

        ready = self.ensure_ready()
        result = self._run(
            [
                ready.cli_path,
                "--profile",
                self.profile,
                "base",
                "+base-create",
                "--as",
                "user",
                "--name",
                clean_name,
                "--time-zone",
                str(time_zone),
            ],
            label="create Base",
            timeout=min(self.timeout, 120.0),
        )
        payload = self._parse_json_output(result, "create Base")

        token = _find_first_string(
            payload,
            ("app_token", "appToken", "base_token", "baseToken", "token"),
        )
        if not token:
            raise LarkCliCommandError(
                "Base was created but the CLI response did not include an app token"
            )

        base_url = _find_first_string(
            payload,
            ("base_url", "baseUrl", "url"),
        )
        if not base_url:
            base_url = f"https://www.feishu.cn/base/{token}"
        _validate_official_https_url(base_url)

        returned_name = _find_first_string(payload, ("name",)) or clean_name
        return {
            "name": returned_name,
            "app_token": token,
            "base_url": base_url,
        }

    def _configure_profile(self, cli: str) -> None:
        self._emit("等待用户在浏览器确认创建飞书应用…")
        self._run_streaming_browser_command(
            [
                cli,
                "config",
                "init",
                "--new",
                "--name",
                self.profile,
                "--brand",
                "feishu",
                "--lang",
                "zh",
            ],
            label="configure CLI profile",
            timeout=self.timeout,
        )
        if not self.profile_configured():
            raise LarkCliCommandError(
                "Feishu application setup completed but the CLI profile is unavailable"
            )

    def _authorize(self, cli: str) -> None:
        self._emit("正在申请飞书多维表格授权…")
        initiate = self._run(
            [
                cli,
                "--profile",
                self.profile,
                "auth",
                "login",
                "--domain",
                "base",
                "--recommend",
                "--no-wait",
                "--json",
            ],
            label="start CLI authorization",
            timeout=min(self.timeout, 60.0),
        )
        payload = self._parse_json_output(initiate, "start CLI authorization")
        verification_url = _find_first_string(
            payload,
            (
                "verification_url_complete",
                "verification_uri_complete",
                "verification_url",
                "verification_uri",
                "verificationUrl",
                "verificationUri",
            ),
        )
        device_code = _find_first_string(payload, ("device_code", "deviceCode"))
        if not verification_url or not device_code:
            raise LarkCliCommandError(
                "CLI authorization response is missing its browser URL or device code"
            )

        self._open_authorization_url(verification_url)
        self._emit("等待用户在浏览器确认飞书授权…")

        expires_in = _find_first_number(payload, ("expires_in", "expiresIn"))
        poll_timeout = self.timeout
        if expires_in is not None and expires_in > 0:
            # Leave a small grace period for final token persistence while never
            # extending the caller's configured upper bound.
            poll_timeout = min(self.timeout, float(expires_in) + 10.0)

        self._run(
            [
                cli,
                "--profile",
                self.profile,
                "auth",
                "login",
                "--device-code",
                device_code,
                "--json",
            ],
            label="complete CLI authorization",
            timeout=poll_timeout,
        )

    def _run_streaming_browser_command(
        self,
        args: Sequence[str],
        *,
        label: str,
        timeout: float,
    ) -> None:
        try:
            process = self._popen_factory(
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except (OSError, ValueError) as exc:
            raise LarkCliCommandError(f"Unable to start {label}") from exc

        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            stream = process.stdout
            if stream is None:
                output_queue.put(None)
                return
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = self._monotonic() + timeout
        reader_done = False
        browser_opened = False
        unsafe_url_seen = False

        try:
            while True:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    raise LarkCliTimeoutError(f"Timed out while waiting for {label}")

                try:
                    item = output_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    item = ""

                if item is None:
                    reader_done = True
                elif item and not browser_opened:
                    for candidate in _extract_https_urls(item):
                        try:
                            _validate_official_https_url(candidate)
                        except UnsafeAuthorizationUrlError:
                            unsafe_url_seen = True
                            continue
                        self._open_authorization_url(candidate)
                        browser_opened = True
                        break

                return_code = process.poll()
                if return_code is not None and reader_done and output_queue.empty():
                    if return_code != 0:
                        raise LarkCliCommandError(
                            f"{label} failed with exit code {return_code}"
                        )
                    if unsafe_url_seen and not browser_opened:
                        raise UnsafeAuthorizationUrlError(
                            "CLI returned a browser URL outside official Feishu/Lark domains"
                        )
                    return
        except Exception:
            if process.poll() is None:
                self._terminate_process(process)
            raise

    def _open_authorization_url(self, url: str) -> None:
        _validate_official_https_url(url)
        if not self._open_browser_enabled:
            self._present_authorization_url(url)
            return
        try:
            opened = self._browser_opener(url)
        except Exception:
            opened = False
        if opened is False:
            # Browser launch support varies on headless Linux and locked-down
            # desktops.  Showing a clickable URL keeps the authorization flow
            # usable without exposing the device code passed to the CLI.
            self._present_authorization_url(url)

    def _present_authorization_url(self, url: str) -> None:
        message = f"请在浏览器打开飞书授权页面：{url}"
        try:
            if self._progress:
                self._progress(message)
            else:
                print(message, flush=True)
        except Exception as exc:
            raise BrowserLaunchError(
                "Unable to present the Feishu authorization page"
            ) from exc

    def _run(
        self,
        args: Sequence[str],
        *,
        label: str,
        timeout: float,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LarkCliTimeoutError(f"Timed out while waiting for {label}") from exc
        except (OSError, ValueError) as exc:
            raise LarkCliCommandError(f"Unable to start {label}") from exc

        if check and result.returncode != 0:
            # Never include argv or subprocess output here: an auth command can
            # carry a device code and output can contain other transient data.
            raise LarkCliCommandError(
                f"{label} failed with exit code {result.returncode}"
            )
        return result

    def _parse_json_output(
        self,
        result: subprocess.CompletedProcess[str],
        label: str,
    ) -> Any:
        combined = "\n".join(
            part for part in (result.stdout or "", result.stderr or "") if part
        )
        documents = _extract_json_documents(combined)
        if not documents:
            raise LarkCliCommandError(f"{label} returned invalid JSON")

        # Prefer an explicitly successful object when update notices or other
        # JSON diagnostics accompany the business response.
        for document in documents:
            if isinstance(document, Mapping) and document.get("ok") is True:
                return document
        return documents[0]

    def _command_indicates_success(
        self,
        result: subprocess.CompletedProcess[str],
    ) -> bool:
        if result.returncode != 0:
            return False
        combined = "\n".join(
            part for part in (result.stdout or "", result.stderr or "") if part
        )
        documents = _extract_json_documents(combined)
        for document in documents:
            for mapping in _iter_mappings(document):
                if mapping.get("ok") is False:
                    return False
                if any(
                    mapping.get(flag) is False
                    for flag in ("verified", "authorized", "authenticated", "valid")
                ):
                    return False
                status = str(
                    mapping.get("tokenStatus")
                    or mapping.get("token_status")
                    or mapping.get("status")
                    or ""
                ).lower()
                if status in {"invalid", "expired", "missing", "unauthorized"}:
                    return False
        lowered = combined.lower()
        failure_markers = (
            "not authenticated",
            "not authorized",
            "token expired",
            "token invalid",
            "profile not found",
        )
        return not any(marker in lowered for marker in failure_markers)

    def _resolve_cli_path(self, *, refresh: bool = False) -> str | None:
        if self._cli_path and not refresh:
            return self._cli_path

        requested = self._requested_cli_path
        if requested:
            expanded = os.path.expandvars(os.path.expanduser(requested))
            if self._path_exists(expanded):
                # Preserve the caller-provided representation.  In particular,
                # do not reinterpret a POSIX path with Windows path semantics
                # when a remote/container executable is injected by an embedder.
                return expanded
            resolved = self._which(expanded)
            if resolved:
                return resolved

        return self._which("lark-cli")

    def _resolve_cli_from_npm_prefix(self) -> str | None:
        npm = self._which("npm")
        if not npm:
            return None
        prefix_result = self._run(
            [npm, "prefix", "-g"],
            label="locate npm global directory",
            timeout=min(self.timeout, 30.0),
            check=False,
        )
        if prefix_result.returncode != 0:
            return None
        prefix_text = (prefix_result.stdout or "").strip().splitlines()
        if not prefix_text:
            return None
        prefix = Path(prefix_text[-1].strip())
        if os.name == "nt":
            candidates = (prefix / "lark-cli.cmd", prefix / "lark-cli.exe")
        else:
            candidates = (prefix / "bin" / "lark-cli", prefix / "lark-cli")
        for candidate in candidates:
            if self._path_exists(str(candidate)):
                return str(candidate)
        return None

    def _require_cli_path(self) -> str:
        path = self._cli_path or self._resolve_cli_path()
        if not path:
            raise LarkCliRuntimeError("lark-cli is not available")
        self._cli_path = path
        return path

    def _emit(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _extract_https_urls(text: str) -> list[str]:
    clean = _ANSI_ESCAPE_RE.sub("", text or "")
    return [
        match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        for match in _HTTPS_URL_RE.finditer(clean)
    ]


def _validate_official_https_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise UnsafeAuthorizationUrlError("CLI returned an invalid browser URL") from exc

    official_host = any(
        host == domain or host.endswith(f".{domain}")
        for domain in OFFICIAL_BROWSER_DOMAINS
    )
    if (
        parsed.scheme.lower() != "https"
        or not official_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise UnsafeAuthorizationUrlError(
            "CLI returned a browser URL outside official Feishu/Lark domains"
        )


def _extract_json_documents(text: str) -> list[Any]:
    """Decode JSON documents embedded in otherwise human-readable CLI output."""

    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    source = _ANSI_ESCAPE_RE.sub("", text or "")
    while index < len(source):
        object_start = source.find("{", index)
        array_start = source.find("[", index)
        starts = [position for position in (object_start, array_start) if position >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            document, consumed = decoder.raw_decode(source[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        documents.append(document)
        index = start + consumed
    return documents


def _find_first_string(value: Any, keys: Sequence[str]) -> str | None:
    wanted = set(keys)
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key, candidate in value.items():
            if key not in wanted:
                found = _find_first_string(candidate, keys)
                if found:
                    return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_first_string(candidate, keys)
            if found:
                return found
    return None


def _find_first_number(value: Any, keys: Sequence[str]) -> float | None:
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return float(candidate)
            if isinstance(candidate, str):
                try:
                    return float(candidate)
                except ValueError:
                    pass
        for candidate in value.values():
            found = _find_first_number(candidate, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_first_number(candidate, keys)
            if found is not None:
                return found
    return None


def _iter_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for candidate in value.values():
            yield from _iter_mappings(candidate)
    elif isinstance(value, list):
        for candidate in value:
            yield from _iter_mappings(candidate)


__all__ = [
    "BrowserLaunchError",
    "DEFAULT_BASE_TIME_ZONE",
    "DEFAULT_PROFILE",
    "DEFAULT_TIMEOUT_SECONDS",
    "LarkCliCommandError",
    "LarkCliError",
    "LarkCliManager",
    "LarkCliRuntimeError",
    "LarkCliTimeoutError",
    "RuntimeStatus",
    "SetupResult",
    "UnsafeAuthorizationUrlError",
]
