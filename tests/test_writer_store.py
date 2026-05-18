"""Tests for the writer store module."""

from __future__ import annotations

from src.writer_store import _normalize_email, writer_store


def test_normalize_email():
    assert _normalize_email("  Alice@Example.COM ") == "alice@example.com"
    assert _normalize_email("bob@test.org") == "bob@test.org"


def test_find_or_create_creates_new_writer():
    store = writer_store
    writer = store.find_or_create("newuser_test@example.com", "Test User")
    assert writer.email == "newuser_test@example.com"
    assert writer.name == "Test User"
    assert len(writer.id) == 32
    assert writer.is_active is True


def test_find_or_create_is_idempotent():
    store = writer_store
    w1 = store.find_or_create("idempotent_test@example.com", "First")
    w2 = store.find_or_create("idempotent_test@example.com", "First")
    assert w1.id == w2.id
    assert w1.email == w2.email


def test_find_or_create_updates_name():
    store = writer_store
    w1 = store.find_or_create("namechange_test@example.com", "Old Name")
    w2 = store.find_or_create("namechange_test@example.com", "New Name")
    assert w2.id == w1.id
    assert w2.name == "New Name"


def test_find_or_create_email_case_insensitive():
    store = writer_store
    w1 = store.find_or_create("CaseTest@Example.COM", "Case Test")
    w2 = store.find_or_create("casetest@example.com", "Case Test")
    assert w1.id == w2.id


def test_get_by_id():
    store = writer_store
    w = store.find_or_create("getbyid_test@example.com", "GetById")
    found = store.get_by_id(w.id)
    assert found is not None
    assert found.email == "getbyid_test@example.com"


def test_get_by_id_not_found():
    assert writer_store.get_by_id("nonexistent_id_0000000000") is None


def test_get_by_email():
    store = writer_store
    store.find_or_create("getbyemail_test@example.com", "GetByEmail")
    found = store.get_by_email("GetByEmail_Test@Example.COM")
    assert found is not None
    assert found.name == "GetByEmail"


def test_list_all():
    store = writer_store
    store.find_or_create("listall_a@example.com", "A User")
    store.find_or_create("listall_b@example.com", "B User")
    writers = store.list_all()
    emails = [w.email for w in writers]
    assert "listall_a@example.com" in emails
    assert "listall_b@example.com" in emails


def test_to_dict():
    w = writer_store.find_or_create("todict_test@example.com", "Dict Test")
    d = w.to_dict()
    assert d["id"] == w.id
    assert d["email"] == "todict_test@example.com"
    assert d["name"] == "Dict Test"
    assert "created_at" in d
    assert "updated_at" in d
