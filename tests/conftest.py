"""Shared pytest fixtures for the Agent Hub kernel tests."""

from __future__ import annotations

import pytest

from agenthub.contracts import Policy
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.db import Database
from agenthub_kernel.service import PolicyService
from agenthub_kernel.worker import Worker


@pytest.fixture
def db():
    db = Database("sqlite:///:memory:")
    db.create_all()
    return db


@pytest.fixture
def adapter():
    return MockAdapter()


@pytest.fixture
def policy():
    return Policy()


@pytest.fixture
def svc(db):
    """A PolicyService bound to a fresh session for the current test."""
    pass  # replaced by factory helper; see make_svc
