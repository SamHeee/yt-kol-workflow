"""Typed, non-interactive wrappers around lark-cli Base shortcuts.

The provisioning workflow uses the CLI's documented ``base +...`` shortcuts
instead of constructing OpenAPI requests itself.  Read operations are exposed
only as list/get helpers.  Mutating helpers inherit the runner's construction-
time ``dry_run`` mode and also accept an explicit per-call override.

Structured field and workflow definitions are passed through short-lived JSON
files.  This keeps potentially large definitions out of process arguments and
avoids quoting differences between PowerShell, cmd.exe, and POSIX shells.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence


DEFAULT_TIMEOUT_SECONDS = 60.0

# These are the shortcuts used by first-run provisioning.  The MVP never
# creates extra views; it only sets and reads the visible-field order of the
# one grid view automatically generated for each newly created table.
REQUIRED_BASE_COMMANDS = (
    "+table-list",
    "+table-get",
    "+table-create",
    "+field-list",
    "+field-get",
    "+field-create",
    "+field-update",
    "+view-list",
    "+view-get",
    "+view-get-visible-fields",
    "+view-set-visible-fields",
    "+workflow-list",
    "+workflow-get",
    "+workflow-create",
    "+workflow-update",
    "+workflow-enable",
    "+workflow-disable",
)

_SEMVER_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
_BASE_TOKEN_RE = re.compile(r"(?i)\b(?:bascn|app)[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s\"',}\]]+")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:device[_-]?code|access[_-]?token|refresh[_-]?token|"
    r"tenant[_-]?access[_-]?token|app[_-]?secret|authorization|"
    r"base[_-]?token|app[_-]?token)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)


class LarkCliBaseError(RuntimeError):
    """Base class for typed lark-cli Base runner failures."""


class LarkCliBaseCommandError(LarkCliBaseError):
    """Raised when lark-cli cannot run or rejects a command."""


class LarkCliBaseTimeoutError(LarkCliBaseError):
    """Raised when a lark-cli command exceeds the configured timeout."""


class LarkCliBaseResponseError(LarkCliBaseError):
    """Raised when lark-cli returns an invalid or unexpected response."""


@dataclass(frozen=True)
class LarkCliCapabilityProbe:
    """Locally detected CLI version and required Base shortcut support."""

    version: str
    available_commands: tuple[str, ...]
    missing_commands: tuple[str, ...]

    @property
    def supported(self) -> bool:
        """Whether every shortcut required by first-run provisioning exists."""

        return not self.missing_commands


RunCallable = Callable[..., subprocess.CompletedProcess[str]]
Identity = Literal["user", "bot"]


class LarkCliBaseRunner:
    """Run typed Base shortcut commands through one explicit profile and Base.

    ``base_token`` and ``profile`` are mandatory keyword arguments on purpose.
    A runner instance therefore cannot silently fall back to another profile or
    target Base.  ``runner`` is injectable for deterministic, offline tests.
    """

    def __init__(
        self,
        *,
        base_token: str,
        profile: str,
        cli_path: str | os.PathLike[str] = "lark-cli",
        identity: Identity = "user",
        dry_run: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        runner: RunCallable | None = None,
        temp_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.base_token = self._required_text(base_token, "base_token")
        self.profile = self._required_text(profile, "profile")
        self.cli_path = self._required_text(os.fspath(cli_path), "cli_path")
        if identity not in ("user", "bot"):
            raise ValueError("identity must be 'user' or 'bot'")
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be a bool")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self.identity: Identity = identity
        self.dry_run = dry_run
        self.timeout = float(timeout)
        self._runner = runner or subprocess.run
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None

    # ----- local capability probe (does not access a Base) -----

    def probe_capabilities(self) -> LarkCliCapabilityProbe:
        """Probe the installed version and shortcut names using local help only."""

        version_output = self._run_text(
            [self.cli_path, "--version"], label="probe lark-cli version"
        )
        help_output = self._run_text(
            [self.cli_path, "base", "--help"], label="probe Base capabilities"
        )
        version_match = _SEMVER_RE.search(version_output)
        version = version_match.group(1) if version_match else "unknown"
        available = tuple(
            command for command in REQUIRED_BASE_COMMANDS if command in help_output
        )
        missing = tuple(
            command for command in REQUIRED_BASE_COMMANDS if command not in help_output
        )
        return LarkCliCapabilityProbe(
            version=version,
            available_commands=available,
            missing_commands=missing,
        )

    # ----- strictly read-only table/field/view/workflow helpers -----

    def list_tables(self) -> list[dict[str, Any]]:
        data = self._execute_json(self._base_command("+table-list"), "list tables")
        return self._extract_items(data, "tables")

    def get_table(self, table_id_or_name: str) -> dict[str, Any]:
        target = self._required_text(table_id_or_name, "table_id_or_name")
        data = self._execute_json(
            self._base_command("+table-get", "--table-id", target), "get table"
        )
        return self._extract_object(data, "table")

    def list_fields(self, table_id_or_name: str) -> list[dict[str, Any]]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        data = self._execute_json(
            self._base_command("+field-list", "--table-id", table), "list fields"
        )
        return self._extract_items(data, "fields")

    def get_field(
        self, table_id_or_name: str, field_id_or_name: str
    ) -> dict[str, Any]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        field = self._required_text(field_id_or_name, "field_id_or_name")
        data = self._execute_json(
            self._base_command(
                "+field-get",
                "--table-id",
                table,
                "--field-id",
                field,
            ),
            "get field",
        )
        return self._extract_object(data, "field")

    def list_views(self, table_id_or_name: str) -> list[dict[str, Any]]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        data = self._execute_json(
            self._base_command("+view-list", "--table-id", table), "list views"
        )
        return self._extract_items(data, "views")

    def get_view(
        self, table_id_or_name: str, view_id_or_name: str
    ) -> dict[str, Any]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        view = self._required_text(view_id_or_name, "view_id_or_name")
        data = self._execute_json(
            self._base_command(
                "+view-get",
                "--table-id",
                table,
                "--view-id",
                view,
            ),
            "get view",
        )
        return self._extract_object(data, "view")

    def get_view_visible_fields(
        self, table_id_or_name: str, view_id_or_name: str
    ) -> dict[str, Any]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        view = self._required_text(view_id_or_name, "view_id_or_name")
        data = self._execute_json(
            self._base_command(
                "+view-get-visible-fields",
                "--table-id",
                table,
                "--view-id",
                view,
            ),
            "get view visible fields",
        )
        return self._extract_object(data, "visible_fields")

    def list_workflows(
        self, *, status: Literal["enabled", "disabled"] | None = None
    ) -> list[dict[str, Any]]:
        if status not in (None, "enabled", "disabled"):
            raise ValueError("status must be 'enabled', 'disabled', or None")
        extra = ("--status", status) if status else ()
        data = self._execute_json(
            self._base_command("+workflow-list", *extra), "list workflows"
        )
        return self._extract_items(data, "workflows")

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._required_text(workflow_id, "workflow_id")
        data = self._execute_json(
            self._base_command("+workflow-get", "--workflow-id", workflow),
            "get workflow",
        )
        return self._extract_object(data, "workflow")

    # ----- explicit dry-run/write provisioning helpers -----

    def create_table(
        self,
        name: str,
        fields: Sequence[Mapping[str, Any]],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Create a table with at least its primary field definition."""

        table_name = self._required_text(name, "name")
        command = self._base_command("+table-create", "--name", table_name)
        normalized_fields = self._mapping_sequence(fields, "fields")
        if not normalized_fields:
            raise ValueError("fields must contain at least the primary field")
        with self._temporary_json(normalized_fields) as (json_arg, command_cwd):
            command.extend(("--fields", json_arg))
            return self._execute_json(
                self._with_effective_dry_run(command, dry_run),
                "create table",
                cwd=command_cwd,
            )

    def create_field(
        self,
        table_id_or_name: str,
        definition: Mapping[str, Any],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        payload = self._mapping(definition, "definition")
        with self._temporary_json(payload) as (json_arg, command_cwd):
            command = self._base_command(
                "+field-create",
                "--table-id",
                table,
                "--json",
                json_arg,
            )
            if self._requires_field_guide(payload):
                command.append("--i-have-read-guide")
            return self._execute_json(
                self._with_effective_dry_run(command, dry_run),
                "create field",
                cwd=command_cwd,
            )

    def update_field(
        self,
        table_id_or_name: str,
        field_id_or_name: str,
        definition: Mapping[str, Any],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Fully replace a field definition after the caller read its baseline."""

        table = self._required_text(table_id_or_name, "table_id_or_name")
        field = self._required_text(field_id_or_name, "field_id_or_name")
        payload = self._mapping(definition, "definition")
        with self._temporary_json(payload) as (json_arg, command_cwd):
            effective_dry_run = self._effective_dry_run(dry_run)
            command = self._base_command(
                "+field-update",
                "--table-id",
                table,
                "--field-id",
                field,
                "--json",
                json_arg,
            )
            if self._requires_field_guide(payload):
                command.append("--i-have-read-guide")
            if not effective_dry_run:
                command.append("--yes")
            return self._execute_json(
                self._with_dry_run(command, effective_dry_run),
                "update field",
                cwd=command_cwd,
            )

    def create_workflow(
        self, definition: Mapping[str, Any], *, dry_run: bool | None = None
    ) -> dict[str, Any]:
        payload = self._mapping(definition, "definition")
        with self._temporary_json(payload) as (json_arg, command_cwd):
            command = self._base_command(
                "+workflow-create", "--json", json_arg
            )
            return self._execute_json(
                self._with_effective_dry_run(command, dry_run),
                "create workflow",
                cwd=command_cwd,
            )

    def create_workflow_idempotent(
        self,
        definition: Mapping[str, Any],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Create once by title, recovering an ambiguous timeout safely.

        A persisted ``client_token`` lets the service deduplicate a retry, but
        a local timeout can still hide a successful response.  Re-list by the
        unique managed title first; only retry when no matching workflow is
        visible, and fail closed if duplicates already exist.
        """

        payload = self._mapping(definition, "definition")
        title = self._required_text(payload.get("title"), "definition.title")
        matches = [
            item
            for item in self.list_workflows()
            if str(item.get("title") or item.get("name") or "") == title
        ]
        if len(matches) > 1:
            raise LarkCliBaseResponseError(
                "multiple workflows already use the managed title"
            )
        if matches:
            return matches[0]
        try:
            return self.create_workflow(payload, dry_run=dry_run)
        except LarkCliBaseTimeoutError:
            matches = [
                item
                for item in self.list_workflows()
                if str(item.get("title") or item.get("name") or "") == title
            ]
            if len(matches) > 1:
                raise LarkCliBaseResponseError(
                    "workflow creation timed out and duplicate titles were found"
                )
            if matches:
                return matches[0]
            # Retry the same exact payload, including client_token.
            return self.create_workflow(payload, dry_run=dry_run)

    def set_view_visible_fields(
        self,
        table_id_or_name: str,
        view_id_or_name: str,
        field_names: Sequence[str],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        table = self._required_text(table_id_or_name, "table_id_or_name")
        view = self._required_text(view_id_or_name, "view_id_or_name")
        names = [self._required_text(name, "field_name") for name in field_names]
        if not names:
            raise ValueError("field_names must not be empty")
        payload = {"visible_fields": names}
        with self._temporary_json(payload) as (json_arg, command_cwd):
            command = self._base_command(
                "+view-set-visible-fields",
                "--table-id",
                table,
                "--view-id",
                view,
                "--json",
                json_arg,
            )
            return self._execute_json(
                self._with_effective_dry_run(command, dry_run),
                "set view visible fields",
                cwd=command_cwd,
            )

    def update_workflow(
        self,
        workflow_id: str,
        definition: Mapping[str, Any],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        workflow = self._required_text(workflow_id, "workflow_id")
        payload = self._mapping(definition, "definition")
        with self._temporary_json(payload) as (json_arg, command_cwd):
            command = self._base_command(
                "+workflow-update",
                "--workflow-id",
                workflow,
                "--json",
                json_arg,
            )
            return self._execute_json(
                self._with_effective_dry_run(command, dry_run),
                "update workflow",
                cwd=command_cwd,
            )

    def enable_workflow(
        self, workflow_id: str, *, dry_run: bool | None = None
    ) -> dict[str, Any]:
        return self._set_workflow_state(workflow_id, "enable", dry_run=dry_run)

    def disable_workflow(
        self, workflow_id: str, *, dry_run: bool | None = None
    ) -> dict[str, Any]:
        return self._set_workflow_state(workflow_id, "disable", dry_run=dry_run)

    def _set_workflow_state(
        self,
        workflow_id: str,
        state: Literal["enable", "disable"],
        *,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        workflow = self._required_text(workflow_id, "workflow_id")
        command = self._base_command(
            f"+workflow-{state}", "--workflow-id", workflow
        )
        return self._execute_json(
            self._with_effective_dry_run(command, dry_run), f"{state} workflow"
        )

    # ----- subprocess and response safety -----

    def _base_command(self, shortcut: str, *extra: str) -> list[str]:
        return [
            self.cli_path,
            "--profile",
            self.profile,
            "base",
            shortcut,
            "--base-token",
            self.base_token,
            "--as",
            self.identity,
            "--format",
            "json",
            *extra,
        ]

    @staticmethod
    def _with_dry_run(command: list[str], dry_run: bool) -> list[str]:
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be a bool")
        if dry_run:
            command.append("--dry-run")
        return command

    def _with_effective_dry_run(
        self, command: list[str], dry_run: bool | None
    ) -> list[str]:
        return self._with_dry_run(command, self._effective_dry_run(dry_run))

    def _effective_dry_run(self, override: bool | None) -> bool:
        if override is None:
            return self.dry_run
        if not isinstance(override, bool):
            raise TypeError("dry_run must be a bool")
        return override

    def _execute_json(
        self,
        command: list[str],
        label: str,
        *,
        cwd: Path | None = None,
    ) -> Any:
        completed = self._run(command, label, cwd=cwd)
        if completed.returncode != 0:
            detail = self._error_detail(completed.stdout or "", completed.stderr or "")
            raise LarkCliBaseCommandError(
                f"{label} failed (exit={completed.returncode}): {detail}"
            )
        try:
            value = self._parse_json(completed.stdout or "")
        except ValueError as exc:
            raise LarkCliBaseResponseError(
                f"{label} returned invalid JSON"
            ) from exc

        if isinstance(value, dict) and "ok" in value:
            if value.get("ok") is not True:
                detail = self._format_cli_error(value.get("error"))
                raise LarkCliBaseCommandError(f"{label} failed: {detail}")
            return value.get("data", {})
        return value

    def _run(
        self,
        command: list[str],
        label: str,
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            kwargs: dict[str, Any] = {
                "input": None,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "capture_output": True,
                "timeout": self.timeout,
                "check": False,
                "shell": False,
            }
            if cwd is not None:
                kwargs["cwd"] = str(cwd)
            return self._runner(
                command,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise LarkCliBaseTimeoutError(
                f"{label} timed out after {self.timeout:g} seconds"
            ) from exc
        except OSError as exc:
            detail = self._sanitize(str(exc))
            raise LarkCliBaseCommandError(f"could not start {label}: {detail}") from exc

    def _run_text(self, command: list[str], label: str) -> str:
        completed = self._run(command, label)
        if completed.returncode != 0:
            detail = self._error_detail(completed.stdout or "", completed.stderr or "")
            raise LarkCliBaseCommandError(
                f"{label} failed (exit={completed.returncode}): {detail}"
            )
        return "\n".join(
            part for part in (completed.stdout or "", completed.stderr or "") if part
        )

    @staticmethod
    def _parse_json(text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty JSON output")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
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
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message") or error.get("msg") or "CLI error"
            detail = self._sanitize(str(message))
            if code is not None:
                return f"{detail} (code={self._sanitize(str(code))})"
            return detail
        return self._sanitize(str(error or "unknown CLI error"))

    def _sanitize(self, value: str) -> str:
        safe = value.replace(self.base_token, "<redacted-base-token>")
        safe = _BASE_TOKEN_RE.sub("<redacted-base-token>", safe)
        safe = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", safe)
        safe = _BEARER_RE.sub("Bearer <redacted>", safe)
        safe = _EMAIL_RE.sub("<redacted-email>", safe)
        safe = " ".join(safe.split())
        return safe[:500] if safe else "no diagnostic output"

    @contextmanager
    def _temporary_json(self, value: Any) -> Iterator[tuple[str, Path]]:
        path: Path | None = None
        try:
            if self._temp_dir is not None:
                self._temp_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                prefix="kol-feishu-",
                dir=str(self._temp_dir) if self._temp_dir is not None else None,
                delete=False,
            )
            path = Path(handle.name)
            try:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
            finally:
                handle.close()
            try:
                path.chmod(0o600)
            except OSError:
                # Windows ACLs do not necessarily map to POSIX mode bits.
                pass
            # lark-cli deliberately rejects @file paths outside its working
            # directory.  Run the command in the temporary file's parent and
            # pass only a local relative name; this also avoids putting JSON
            # payloads on the process command line.
            yield f"@{path.name}", path.parent
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # Do not hide the command result with a cleanup-only error.
                    pass

    # ----- response and argument shape validation -----

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError(f"{name} must not be empty")
        return clean

    @staticmethod
    def _mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")
        return dict(value)

    @staticmethod
    def _requires_field_guide(definition: Mapping[str, Any]) -> bool:
        return str(definition.get("type") or "").strip().lower() in {
            "formula",
            "lookup",
        }

    @classmethod
    def _mapping_sequence(
        cls, value: Sequence[Mapping[str, Any]], name: str
    ) -> list[dict[str, Any]]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError(f"{name} must be a sequence of mappings")
        return [cls._mapping(item, f"{name}[{index}]") for index, item in enumerate(value)]

    @staticmethod
    def _extract_items(data: Any, resource_name: str) -> list[dict[str, Any]]:
        if isinstance(data, list):
            items = data
        elif isinstance(data, Mapping):
            items = None
            for key in ("items", resource_name):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
            if items is None:
                raise LarkCliBaseResponseError(
                    f"CLI response did not contain a {resource_name} list"
                )
        else:
            raise LarkCliBaseResponseError(
                f"CLI response did not contain a {resource_name} list"
            )

        if not all(isinstance(item, Mapping) for item in items):
            raise LarkCliBaseResponseError(
                f"CLI response contained an invalid {resource_name} item"
            )
        return [dict(item) for item in items]

    @staticmethod
    def _extract_object(data: Any, resource_name: str) -> dict[str, Any]:
        if isinstance(data, Mapping):
            nested = data.get(resource_name)
            if isinstance(nested, Mapping):
                return dict(nested)
            return dict(data)
        raise LarkCliBaseResponseError(
            f"CLI response did not contain a {resource_name} object"
        )


__all__ = [
    "LarkCliBaseCommandError",
    "LarkCliBaseError",
    "LarkCliBaseResponseError",
    "LarkCliBaseRunner",
    "LarkCliBaseTimeoutError",
    "LarkCliCapabilityProbe",
    "REQUIRED_BASE_COMMANDS",
]
