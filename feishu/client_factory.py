#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create one Feishu Bitable client for CLI-user or legacy app auth.

The factory also remembers a non-secret Base target per lark-cli profile.  A
resource token does not grant access by itself; credentials remain exclusively
in the operating-system keychain managed by lark-cli.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .auth import FeishuAuth
from .bitable import BitableClient


logger = logging.getLogger("kol_workflow.feishu.client_factory")

DEFAULT_TARGET_STORE = Path.home() / ".kol_workflow" / "feishu_base_targets.json"


@dataclass
class BaseTarget:
    app_token: str
    url: str = ""
    name: str = ""
    auth_mode: str = "cli"
    created_at: str = ""


@dataclass
class FeishuClientContext:
    client: BitableClient
    app_token: str
    auth_mode: str
    base_url: str = ""
    base_name: str = ""
    created_base: bool = False
    cli_manager: Any = None


def extract_app_token(url_or_token: str) -> str:
    """Extract a Base token while retaining compatibility with old inputs."""
    value = (url_or_token or "").strip()
    if not value:
        return ""
    match = re.search(r"/(?:base|wiki)/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def create_bitable_client(
    *,
    app_token: str = "",
    app_id: str = "",
    app_secret: str = "",
    auth_mode: str = "auto",
    cli_profile: str = "kol-workflow",
    cli_path: str = "",
    auto_setup: bool = True,
    auto_install: bool = True,
    open_browser: bool = True,
    timeout: int = 900,
    create_base_if_missing: bool = True,
    base_name: str = "KOL网红开发工作流",
    target_store: Optional[Path] = None,
    manager_factory: Optional[Callable[..., Any]] = None,
    cli_client_class: Optional[type] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> FeishuClientContext:
    """Create a client and, in CLI mode, prepare a reusable Base target."""
    mode = (auth_mode or "auto").strip().lower()
    if mode not in {"auto", "cli", "app"}:
        raise ValueError("飞书认证模式必须是 auto、cli 或 app")
    if timeout <= 0:
        raise ValueError("飞书授权超时必须大于 0 秒")

    store_path = Path(target_store or DEFAULT_TARGET_STORE).expanduser()
    supplied_value = (app_token or "").strip()
    supplied_url = supplied_value if supplied_value.startswith(("https://", "http://")) else ""
    token = extract_app_token(supplied_value)
    stored = None if token else load_base_target(cli_profile, store_path)
    if stored:
        token = stored.app_token

    if mode == "auto":
        if stored and stored.auth_mode == "cli":
            mode = "cli"
        elif token and app_id and app_secret:
            mode = "app"
        else:
            mode = "cli"

    if mode == "app":
        missing = []
        if not token:
            missing.append("app_token")
        if not app_id:
            missing.append("APP_ID")
        if not app_secret:
            missing.append("APP_SECRET")
        if missing:
            raise RuntimeError("飞书 App 模式缺少配置: " + ", ".join(missing))
        client = BitableClient(token, FeishuAuth(app_id, app_secret))
        return FeishuClientContext(
            client=client,
            app_token=token,
            auth_mode="app",
            base_url=supplied_url,
            base_name=base_name,
        )

    if manager_factory is None:
        from .lark_cli import LarkCliManager

        manager_factory = LarkCliManager
    if cli_client_class is None:
        from .lark_cli_bitable import LarkCliBitableClient

        cli_client_class = LarkCliBitableClient

    manager = manager_factory(
        profile=cli_profile,
        cli_path=cli_path or None,
        timeout=timeout,
        open_browser=open_browser,
        auto_install=auto_install,
        progress=progress,
    )
    ready_result = _ensure_manager_ready(manager, allow_setup=auto_setup)

    created_base = False
    base_url = supplied_url or (stored.url if stored else "")
    actual_name = (stored.name if stored and stored.name else base_name)
    if not token:
        if not create_base_if_missing:
            raise RuntimeError("未提供飞书 Base URL/app_token，且已禁止自动创建多维表格")
        created = manager.create_base(base_name)
        target = _coerce_created_target(created, default_name=base_name)
        token = target.app_token
        base_url = target.url
        actual_name = target.name or base_name
        created_base = True

    if not token:
        raise RuntimeError("飞书 CLI 未返回可用的 Base app_token")

    target = BaseTarget(
        app_token=token,
        url=base_url,
        name=actual_name,
        auth_mode="cli",
        created_at=(
            stored.created_at
            if stored and not created_base
            else datetime.now(timezone.utc).isoformat()
        ),
    )
    save_base_target(cli_profile, target, store_path)

    resolved_cli_path = (
        getattr(ready_result, "cli_path", "")
        or getattr(manager, "cli_path", "")
        or getattr(manager, "executable", "")
        or cli_path
        or "lark-cli"
    )
    client = cli_client_class(
        token,
        cli_path=resolved_cli_path,
        profile=cli_profile,
        timeout=min(max(timeout, 30), 300),
    )
    return FeishuClientContext(
        client=client,
        app_token=token,
        auth_mode="cli",
        base_url=base_url,
        base_name=actual_name,
        created_base=created_base,
        cli_manager=manager,
    )


def create_bitable_client_from_config(
    config: Any,
    *,
    create_base_if_missing: bool = True,
    target_store: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> FeishuClientContext:
    """Convenience adapter for :class:`config.FeishuConfig`."""
    return create_bitable_client(
        app_token=getattr(config, "app_token", ""),
        app_id=getattr(config, "app_id", ""),
        app_secret=getattr(config, "app_secret", ""),
        auth_mode=getattr(config, "auth_mode", "auto"),
        cli_profile=getattr(config, "cli_profile", "kol-workflow"),
        cli_path=getattr(config, "cli_path", ""),
        auto_setup=bool(getattr(config, "auto_setup", True)),
        auto_install=bool(getattr(config, "auto_install", True)),
        open_browser=bool(getattr(config, "open_browser", True)),
        timeout=int(getattr(config, "auth_timeout_seconds", 900)),
        create_base_if_missing=create_base_if_missing,
        base_name=getattr(config, "base_name", "KOL网红开发工作流"),
        target_store=target_store,
        progress=progress,
    )


def initialize_created_base_schema(context: FeishuClientContext) -> dict:
    """Create the four business tables and remove a new Base's blank default table."""
    if not context.created_base:
        return {}

    from .schema import (
        INFLUENCERS_TABLE,
        INFLUENCER_VIDEOS_TABLE,
        SEARCH_TASKS_TABLE,
        SEARCH_VIDEOS_TABLE,
        SchemaManager,
    )

    client = context.client
    initial_tables = client.list_tables()
    table_ids = SchemaManager(client).ensure_all_tables()
    business_names = {
        SEARCH_TASKS_TABLE,
        SEARCH_VIDEOS_TABLE,
        INFLUENCERS_TABLE,
        INFLUENCER_VIDEOS_TABLE,
    }
    business_ids = set(table_ids.values())
    for table in initial_tables:
        table_id = str(table.get("table_id") or "")
        table_name = str(table.get("name") or "")
        if table_id and table_id not in business_ids and table_name not in business_names:
            try:
                client.delete_table(table_id)
            except Exception as exc:
                logger.warning("自动清理新 Base 的默认空表失败: %s", exc)
    return table_ids


def load_base_target(profile: str, path: Path = DEFAULT_TARGET_STORE) -> Optional[BaseTarget]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = (payload.get("profiles") or {}).get(profile) or {}
        token = extract_app_token(str(item.get("app_token") or ""))
        if not token:
            return None
        return BaseTarget(
            app_token=token,
            url=str(item.get("url") or ""),
            name=str(item.get("name") or ""),
            auth_mode=str(item.get("auth_mode") or "cli"),
            created_at=str(item.get("created_at") or ""),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_base_target(
    profile: str,
    target: BaseTarget,
    path: Path = DEFAULT_TARGET_STORE,
) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "profiles": {}}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            payload.update(current)
            payload.setdefault("profiles", {})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    payload["profiles"][profile] = asdict(target)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    temp_path.replace(path)


def _ensure_manager_ready(manager: Any, *, allow_setup: bool) -> Any:
    if allow_setup:
        result = manager.ensure_ready()
        if result is False:
            raise RuntimeError("飞书 CLI 授权未通过")
        return result
    ensure_cli = getattr(manager, "ensure_cli", None)
    if callable(ensure_cli):
        ensure_cli()
    configured = getattr(manager, "profile_configured", None)
    authorized = getattr(manager, "authorization_valid", None)
    if callable(configured) and callable(authorized):
        if not configured() or not authorized():
            raise RuntimeError("飞书 CLI 尚未授权，且自动设置已禁用")
        return None
    for name in ("verify_ready", "is_ready"):
        method = getattr(manager, name, None)
        if callable(method):
            if not method():
                raise RuntimeError("飞书 CLI 尚未授权，且自动设置已禁用")
            return None
    raise RuntimeError("自动设置已禁用，无法确认飞书 CLI 授权状态")


def _coerce_created_target(value: Any, *, default_name: str) -> BaseTarget:
    if isinstance(value, BaseTarget):
        return value
    if hasattr(value, "app_token"):
        return BaseTarget(
            app_token=extract_app_token(str(getattr(value, "app_token", ""))),
            url=str(getattr(value, "url", "") or ""),
            name=str(getattr(value, "name", "") or default_name),
        )
    if not isinstance(value, dict):
        raise RuntimeError("飞书 CLI 创建 Base 后返回了无法识别的结果")

    candidates = [value]
    for key in ("data", "app", "base"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for item in candidates:
        token = extract_app_token(
            str(
                item.get("app_token")
                or item.get("base_token")
                or item.get("token")
                or item.get("url")
                or ""
            )
        )
        if token:
            return BaseTarget(
                app_token=token,
            url=str(
                item.get("base_url")
                or item.get("baseUrl")
                or item.get("url")
                or value.get("base_url")
                or value.get("baseUrl")
                or value.get("url")
                or ""
            ),
                name=str(item.get("name") or value.get("name") or default_name),
            )
    raise RuntimeError("飞书 CLI 创建 Base 后未返回 app_token")


def mask_app_token(value: str) -> str:
    if not value:
        return ""
    return "***" if len(value) <= 10 else f"{value[:4]}...{value[-4:]}"
