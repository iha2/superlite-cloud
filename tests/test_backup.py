import os
import sqlite3
import stat

import pytest

from slcloud import backup


def make_db(path, value):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS t(v TEXT)")
    c.execute("DELETE FROM t")
    c.execute("INSERT INTO t VALUES(?)", (value,))
    c.commit()
    c.close()


def val(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT v FROM t").fetchone()[0]
    finally:
        c.close()


def test_backup_mode(tmp_path):
    db, out = str(tmp_path / "a.db"), str(tmp_path / "b.bak")
    make_db(db, "one")
    backup.backup(db, out)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert val(out) == "one"


def test_restore_roundtrip(tmp_path):
    db, out = str(tmp_path / "a.db"), str(tmp_path / "b.bak")
    make_db(db, "one")
    backup.backup(db, out)
    make_db(db, "two")
    backup.restore(db, out, lambda: False)
    assert val(db) == "one"
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_restore_refuses_running(tmp_path):
    db, out = str(tmp_path / "a.db"), str(tmp_path / "b.bak")
    make_db(db, "one")
    backup.backup(db, out)
    make_db(db, "two")
    with pytest.raises(backup.Refused):
        backup.restore(db, out, lambda: True)
    assert val(db) == "two"


def test_restore_refuses_garbage(tmp_path):
    db, bad = str(tmp_path / "a.db"), str(tmp_path / "bad")
    make_db(db, "one")
    (tmp_path / "bad").write_text("not a database" * 50)
    with pytest.raises(backup.Refused):
        backup.restore(db, bad, lambda: False)
    assert val(db) == "one"
