"""Tests for `api.auth`: the password credential store and sessions."""

import json

import pytest

from api.auth import (
    AuthStore,
    SessionManager,
    clear_session_cookie_header,
    read_session_token,
    session_cookie_header,
)


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.json")


# ----------------------------------------------------------------------
# AuthStore
# ----------------------------------------------------------------------


def test_password_is_not_set_initially(store):
    assert store.is_password_set() is False
    assert store.verify_password("anything") is False


def test_set_password_then_verify_it(store):
    store.set_password("correct-horse")

    assert store.is_password_set() is True
    assert store.verify_password("correct-horse") is True
    assert store.verify_password("wrong-password") is False


def test_password_cannot_be_set_twice(store):
    store.set_password("first-password")

    with pytest.raises(ValueError, match="already set"):
        store.set_password("second-password")

    assert store.verify_password("first-password") is True  # unchanged


def test_password_must_meet_a_minimum_length(store):
    with pytest.raises(ValueError, match="at least 4 characters"):
        store.set_password("abc")
    assert store.is_password_set() is False


def test_empty_password_is_rejected(store):
    with pytest.raises(ValueError):
        store.set_password("")
    assert store.is_password_set() is False


def test_the_stored_file_never_contains_the_plain_password(store):
    store.set_password("correct-horse-battery-staple")

    raw = store._path.read_text(encoding="utf-8")
    assert "correct-horse-battery-staple" not in raw
    data = json.loads(raw)
    assert set(data.keys()) == {"salt", "hash"}


def test_a_new_store_over_the_same_file_sees_the_password(tmp_path):
    path = tmp_path / "auth.json"
    AuthStore(path).set_password("correct-horse")

    reopened = AuthStore(path)
    assert reopened.is_password_set() is True
    assert reopened.verify_password("correct-horse") is True


# ----------------------------------------------------------------------
# SessionManager
# ----------------------------------------------------------------------


def test_a_fresh_manager_validates_nothing():
    manager = SessionManager()
    assert manager.is_valid(None) is False
    assert manager.is_valid("made-up-token") is False


def test_created_session_is_valid_until_invalidated():
    manager = SessionManager()
    token = manager.create_session()

    assert manager.is_valid(token) is True

    manager.invalidate(token)
    assert manager.is_valid(token) is False


def test_each_session_gets_a_distinct_unguessable_token():
    manager = SessionManager()
    tokens = {manager.create_session() for _ in range(20)}
    assert len(tokens) == 20  # no collisions
    assert all(len(t) >= 32 for t in tokens)


def test_invalidating_an_unknown_token_does_not_raise():
    SessionManager().invalidate("never-issued")  # just must not raise


# ----------------------------------------------------------------------
# Cookie helpers
# ----------------------------------------------------------------------


def test_session_cookie_header_round_trips_through_read_session_token():
    header = session_cookie_header("abc123")
    assert read_session_token(header.split(";")[0]) == "abc123"


def test_read_session_token_from_a_realistic_cookie_header():
    assert read_session_token("session=xyz; other=1") == "xyz"
    assert read_session_token("other=1; session=xyz") == "xyz"


def test_read_session_token_with_no_cookie_header_is_none():
    assert read_session_token(None) is None
    assert read_session_token("") is None


def test_read_session_token_with_no_session_cookie_present_is_none():
    assert read_session_token("other=1; another=2") is None


def test_clear_session_cookie_header_expires_it():
    header = clear_session_cookie_header()
    assert "Max-Age=0" in header
