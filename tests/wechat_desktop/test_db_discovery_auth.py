"""账号认证回归：仅使用合成加密库和进程替身。"""

from pathlib import Path

import pytest

from channel.wechat_desktop.db import keys
from channel.wechat_desktop.db.discovery import DatabaseCatalog
from channel.wechat_desktop.db.errors import DatabaseReadError

from .test_db_crypto import FAKE_KEY, encrypt_page, plain_page


OTHER_KEY = bytes(reversed(FAKE_KEY))
MAIN_PID = 42
HELPER_PID = 43


def _contact(account_dir, relative, key=FAKE_KEY):
    path = account_dir / "db_storage" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt_page(plain_page(), key=key))
    return path


def _catalog(root, monkeypatch, candidates=None, processes=None):
    candidates = [(MAIN_PID, FAKE_KEY)] if candidates is None else candidates
    processes = [(MAIN_PID, "main-version")] if processes is None else processes
    monkeypatch.setattr(keys, "scan_key_candidates", lambda *_: candidates)
    monkeypatch.setattr(keys, "process_is_alive", lambda *_: True)
    monkeypatch.setattr(keys, "candidate_is_resident", lambda *_: True)
    return DatabaseCatalog({"db_data_dir": str(root), "db_key_scan_timeout_seconds": 1},
                           processes=processes)


def _contact_order(monkeypatch, account_dir, contacts):
    """固定操作系统未保证的遍历顺序，确保旧库/错误候选在先。"""
    original = Path.rglob
    storage = account_dir / "db_storage"

    def ordered(path, pattern):
        if path == storage and pattern == "contact.db":
            return iter(contacts)
        return original(path, pattern)

    monkeypatch.setattr(Path, "rglob", ordered)


def test_resolve_ignores_migrated_contact_when_it_is_enumerated_first(tmp_path, monkeypatch):
    account = tmp_path / "synthetic-account"
    migrated = _contact(account, "migrate/contact/contact.db", OTHER_KEY)
    current = _contact(account, "contact/contact.db")
    _contact_order(monkeypatch, account, [migrated, current])
    catalog = _catalog(tmp_path, monkeypatch)

    binding = catalog.resolve()

    assert binding.account_dir == account
    assert binding.pid == MAIN_PID
    assert catalog.resolve() is binding


@pytest.mark.parametrize("has_current", [False, True])
def test_migrated_contact_cannot_authenticate_an_account(tmp_path, monkeypatch, has_current):
    account = tmp_path / "synthetic-account"
    migrated = _contact(account, "migrate/contact/contact.db")
    contacts = [migrated]
    if has_current:
        contacts.append(_contact(account, "contact/contact.db", OTHER_KEY))
    _contact_order(monkeypatch, account, contacts)
    catalog = _catalog(tmp_path, monkeypatch)

    with pytest.raises(DatabaseReadError) as error:
        catalog.resolve()

    assert error.value.code == "key_not_found"


def test_resolve_authenticates_all_current_contact_candidates(tmp_path, monkeypatch):
    account = tmp_path / "synthetic-account"
    wrong = _contact(account, "a/contact.db", OTHER_KEY)
    correct = _contact(account, "b/contact.db")
    _contact_order(monkeypatch, account, [wrong, correct])
    catalog = _catalog(tmp_path, monkeypatch)

    binding = catalog.resolve()

    assert binding.pid == MAIN_PID
    assert catalog.validate_binding(binding)


def test_cached_binding_rechecks_all_current_contacts(tmp_path, monkeypatch):
    account = tmp_path / "synthetic-account"
    correct = _contact(account, "b/contact.db")
    catalog = _catalog(tmp_path, monkeypatch)
    binding = catalog.resolve()
    migrated = _contact(account, "migrate/contact.db", OTHER_KEY)
    wrong = _contact(account, "a/contact.db", OTHER_KEY)
    _contact_order(monkeypatch, account, [migrated, wrong, correct])

    assert catalog.resolve() is binding


def test_cached_binding_cannot_be_revalidated_by_migrated_contact(tmp_path, monkeypatch):
    account = tmp_path / "synthetic-account"
    current = _contact(account, "contact/contact.db")
    catalog = _catalog(tmp_path, monkeypatch)
    catalog.resolve()
    current.write_bytes(encrypt_page(plain_page(), key=OTHER_KEY))
    migrated = _contact(account, "migrate/contact.db")
    _contact_order(monkeypatch, account, [migrated, current])

    with pytest.raises(DatabaseReadError) as error:
        catalog.resolve()

    assert error.value.code == "login_process_changed"
    assert catalog.key_candidates == ()


@pytest.mark.parametrize("reverse_candidates", [False, True])
def test_same_account_uses_main_process_priority_and_revalidates_its_pid(
        tmp_path, monkeypatch, reverse_candidates):
    account = tmp_path / "synthetic-account"
    _contact(account, "contact/contact.db")
    candidates = [(MAIN_PID, FAKE_KEY), (HELPER_PID, FAKE_KEY)]
    if reverse_candidates:
        candidates.reverse()
    catalog = _catalog(tmp_path, monkeypatch, candidates,
                       [(MAIN_PID, "main-version"), (HELPER_PID, "helper-version")])

    binding = catalog.resolve()

    assert binding.pid == MAIN_PID
    assert binding.version == "main-version"
    resident_checks = []
    monkeypatch.setattr(keys, "candidate_is_resident",
                        lambda pid, key: resident_checks.append((pid, key)) or True)
    assert catalog.resolve() is binding
    assert resident_checks == [(MAIN_PID, FAKE_KEY)]


def test_main_process_priority_applies_across_contact_candidates(tmp_path, monkeypatch):
    account = tmp_path / "synthetic-account"
    helper_contact = _contact(account, "a/contact.db", OTHER_KEY)
    main_contact = _contact(account, "b/contact.db")
    _contact_order(monkeypatch, account, [helper_contact, main_contact])
    catalog = _catalog(tmp_path, monkeypatch,
                       [(HELPER_PID, OTHER_KEY), (MAIN_PID, FAKE_KEY)],
                       [(MAIN_PID, "main-version"), (HELPER_PID, "helper-version")])

    assert catalog.resolve().pid == MAIN_PID


def test_process_priority_does_not_hide_different_account_ambiguity(tmp_path, monkeypatch):
    _contact(tmp_path / "synthetic-account-a", "contact/contact.db")
    _contact(tmp_path / "synthetic-account-b", "contact/contact.db", OTHER_KEY)
    catalog = _catalog(tmp_path, monkeypatch,
                       [(MAIN_PID, FAKE_KEY), (HELPER_PID, OTHER_KEY)],
                       [(MAIN_PID, "main-version"), (HELPER_PID, "helper-version")])

    with pytest.raises(DatabaseReadError) as error:
        catalog.resolve()

    assert error.value.code == "account_ambiguous"
