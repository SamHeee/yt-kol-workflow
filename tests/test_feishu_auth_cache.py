import json
import logging
import os
import stat
import time

import pytest

import feishu.auth as auth_module
from feishu.auth import FeishuAuth


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "private-cache"
    monkeypatch.setattr(auth_module, "_CACHE_DIR", cache_dir)
    monkeypatch.delenv("FEISHU_ALLOW_INSECURE_TOKEN_CACHE", raising=False)
    return cache_dir


def test_windows_default_keeps_token_only_in_memory(monkeypatch, isolated_cache):
    monkeypatch.setattr(auth_module, "_is_windows", lambda: True)

    class Response:
        @staticmethod
        def json():
            return {"code": 0, "tenant_access_token": "process-only", "expire": 7200}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(auth_module.requests, "post", fake_post)
    client = FeishuAuth("legacy-app", "legacy-secret")

    assert client.get_token() == "process-only"
    assert client.get_token() == "process-only"
    assert len(calls) == 1
    assert not isolated_cache.exists()


def test_windows_plaintext_cache_requires_explicit_opt_in(monkeypatch, isolated_cache):
    monkeypatch.setattr(auth_module, "_is_windows", lambda: True)

    assert FeishuAuth("legacy-app", "legacy-secret")._disk_cache is False

    monkeypatch.setenv("FEISHU_ALLOW_INSECURE_TOKEN_CACHE", "true")
    assert FeishuAuth("legacy-app", "legacy-secret")._disk_cache is True


def test_posix_cache_uses_private_permissions_and_atomic_replace(
    monkeypatch, isolated_cache
):
    if os.name == "nt":
        pytest.skip("POSIX permission semantics are verified on POSIX CI")
    monkeypatch.setattr(auth_module, "_is_windows", lambda: False)
    replacements = []
    real_replace = auth_module.os.replace

    def recording_replace(source, destination):
        replacements.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(auth_module.os, "replace", recording_replace)
    client = FeishuAuth("legacy-app", "legacy-secret")
    client._save_token_cache("private-token", time.time() + 3600)

    cache_file = client._token_cache_path()
    assert cache_file.exists()
    assert stat.S_IMODE(isolated_cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == isolated_cache
    assert destination == cache_file
    assert not temporary.exists()
    assert json.loads(cache_file.read_text(encoding="utf-8"))["token"] == "private-token"


def test_posix_unsafe_existing_cache_is_ignored(monkeypatch, isolated_cache, caplog):
    monkeypatch.setattr(auth_module, "_is_windows", lambda: False)
    isolated_cache.mkdir(mode=0o700)
    monkeypatch.setattr(auth_module, "_ensure_cache_dir", lambda: True)
    client = FeishuAuth("legacy-app", "legacy-secret")
    cache_file = client._token_cache_path()
    secret = "must-not-appear-in-logs"
    cache_file.write_text(
        json.dumps({"token": secret, "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )
    monkeypatch.setattr(auth_module, "_cache_file_is_secure", lambda _: False)

    with caplog.at_level(logging.WARNING, logger="kol_workflow.feishu.auth"):
        assert client._load_cached_token() == (None, 0)

    assert "Unsafe token cache file ignored" in caplog.text
    assert secret not in caplog.text
    assert str(cache_file) not in caplog.text


def test_posix_secure_existing_cache_can_be_loaded(monkeypatch, isolated_cache):
    if os.name == "nt":
        pytest.skip("POSIX permission semantics are verified on POSIX CI")
    monkeypatch.setattr(auth_module, "_is_windows", lambda: False)
    client = FeishuAuth("legacy-app", "legacy-secret")
    expires_at = time.time() + 3600
    client._save_token_cache("cached-token", expires_at)

    token, cached_expiry = client._load_cached_token()

    assert token == "cached-token"
    assert cached_expiry == pytest.approx(expires_at)


def test_cache_failure_log_does_not_include_token_or_path(
    monkeypatch, isolated_cache, caplog
):
    monkeypatch.setattr(auth_module, "_is_windows", lambda: False)
    monkeypatch.setattr(auth_module, "_ensure_cache_dir", lambda: True)
    client = FeishuAuth("legacy-app", "legacy-secret")
    secret = "do-not-log-this-token"

    def fail_mkstemp(*args, **kwargs):
        raise OSError(f"failed for {secret} at {isolated_cache}")

    monkeypatch.setattr(auth_module.tempfile, "mkstemp", fail_mkstemp)
    with caplog.at_level(logging.WARNING, logger="kol_workflow.feishu.auth"):
        client._save_token_cache(secret, time.time() + 3600)

    assert "Token could not be cached (OSError)" in caplog.text
    assert secret not in caplog.text
    assert str(isolated_cache) not in caplog.text


def test_invalidate_removes_legacy_windows_cache_when_new_writes_are_disabled(
    monkeypatch, isolated_cache
):
    monkeypatch.setattr(auth_module, "_is_windows", lambda: True)
    isolated_cache.mkdir()
    client = FeishuAuth("legacy-app", "legacy-secret")
    cache_file = client._token_cache_path()
    cache_file.write_text("legacy plaintext", encoding="utf-8")

    client.invalidate()

    assert not cache_file.exists()
