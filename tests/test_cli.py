import json

import pytest

from idemkey import DedupStore, FingerprintMismatchError, guard
from idemkey.cli import main


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "cli.db")
    store = DedupStore(path)

    @guard(store=store, sleep=lambda _: None)
    def charge(amount):
        return {"charged": amount}

    charge(100, idemkey="ord-1")
    charge(200, idemkey="ord-2")
    with pytest.raises(FingerprintMismatchError):
        charge(999, idemkey="ord-2")  # same key, different args
    store.close()
    return path


def test_log(capsys, db):
    main(["--store", db, "log"])
    out = capsys.readouterr().out
    assert "ord-1" in out and "ord-2" in out
    assert "completed" in out


def test_log_status_filter(capsys, db):
    main(["--store", db, "log", "--status", "failed"])
    out = capsys.readouterr().out
    assert "(no entries)" in out


def test_show(capsys, db):
    main(["--store", db, "show", "ord-1"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["key"] == "ord-1"
    assert data["result"] == {"charged": 100}
    assert data["status"] == "completed"


def test_show_missing(capsys, db):
    with pytest.raises(SystemExit):
        main(["--store", db, "show", "ghost"])


def test_stats(capsys, db):
    main(["--store", db, "stats"])
    out = capsys.readouterr().out
    assert "total keys: 2" in out
    assert "completed" in out


def test_purge(capsys, db, tmp_path):
    store = DedupStore(db)
    with store._lock:
        store._conn.execute("UPDATE idem_keys SET updated_at = 0")
        store._conn.commit()
    store.close()
    main(["--store", db, "purge", "--older-than", "1"])
    out = capsys.readouterr().out
    assert "purged 2 entries" in out


def test_missing_store_file(tmp_path):
    with pytest.raises(SystemExit):
        main(["--store", str(tmp_path / "nope.db"), "log"])
