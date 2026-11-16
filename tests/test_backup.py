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


def test_backup_does_not_follow_planted_symlink(tmp_path):
    db, out = str(tmp_path / "a.db"), str(tmp_path / "b.bak")
    make_db(db, "one")
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.symlink(victim, f"{out}.{os.getpid()}.tmp")  # the old predictable temp name
    backup.backup(db, out)
    assert victim.read_text() == "keep"
    assert val(out) == "one"


def test_temp_is_exclusive_and_private(tmp_path):
    a = backup._temp(str(tmp_path / "o"), ".tmp")
    b = backup._temp(str(tmp_path / "o"), ".tmp")
    assert a != b
    assert stat.S_IMODE(os.stat(a).st_mode) == 0o600


def test_restore_refused_while_shared_lock_held(tmp_path):
    db, out = str(tmp_path / "a.db"), str(tmp_path / "b.bak")
    make_db(db, "one")
    backup.backup(db, out)
    make_db(db, "two")
    held = backup.hold_shared(db)  # a server or worker process
    try:
        with pytest.raises(backup.Refused):
            backup.restore(db, out, lambda: False)
        assert val(db) == "two"
    finally:
        held.close()
    backup.restore(db, out, lambda: False)
    assert val(db) == "one"


def test_shared_holders_coexist(tmp_path):
    db = str(tmp_path / "a.db")
    make_db(db, "one")
    a, b = backup.hold_shared(db), backup.hold_shared(db)
    a.close()
    b.close()
