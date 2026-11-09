"""Tests must mock external HTTP explicitly, including newly added API paths."""

import pytest
import requests


@pytest.fixture(autouse=True)
def block_unmocked_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unmocked external HTTP is forbidden in offline tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
