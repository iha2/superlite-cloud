import pytest

from slcloud import members as real_members
from slcloud import runs
from slcloud.auth import Principal
from slcloud.engine import Engine
from slcloud.errors import NotFound
from slcloud.store import Store

REPO = "https://github.com/acme/app"


@pytest.fixture
def env(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    eng = Engine(st)
    ids = {h: real_members.create_member(st, "w_ab12", h, f"{h}@example.com", "admin" if h == "root" else "member")
           for h in ("chris", "pat", "root", "sam")}
    ids["other"] = real_members.create_member(st, "w_zz", "other", "other@example.com", "member")
    who = {h: Principal("w_ab12", "admin" if h == "root" else "member", ids[h], h)
           for h in ("chris", "pat", "root", "sam")}
    return st, eng, ids, who


def sub(eng, owner, prompt="Fix the login redirect"):
    return eng.submit("w_ab12", REPO, prompt, "claude", "main", owner=owner)


def test_get_run_non_owner(env):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    assert runs.get_run(st, who["chris"], "login_fix").id == t.id
    assert runs.get_run(st, who["chris"], "chris/login_fix").id == t.id
    assert runs.get_run(st, who["chris"], t.id).id == t.id
    assert runs.get_run(st, who["root"], "chris/login_fix").id == t.id
    for ref in ("login_fix", "chris/login_fix", t.id, "nope", "chris/nope", "ghost/login_fix", ""):
        if ref in ("login_fix", "chris/login_fix", t.id):
            with pytest.raises(NotFound):
                runs.get_run(st, who["pat"], ref)
        else:
            with pytest.raises(NotFound):
                runs.get_run(st, who["root"], ref)
    # admin's bare name means admin's own run, not chris's
    with pytest.raises(NotFound):
        runs.get_run(st, who["root"], "login_fix")


def test_get_run_other_workspace(env):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    outsider = Principal("w_zz", "admin", ids["other"], "other")
    with pytest.raises(NotFound):
        runs.get_run(st, outsider, t.id)


def test_give(env):
    st, eng, ids, who = env
    sub(eng, ids["pat"])  # pat already has login_fix
    t = sub(eng, ids["chris"])
    assert t.name == "login_fix_2"
    sub_ = sub(eng, ids["sam"], "Dark mode for settings")
    g = runs.give(st, eng, who["sam"], "dark_mode", "PAT@example.com")
    assert (g.owner, g.name) == (ids["pat"], "dark_mode")
    # clash: chris gives login_fix_2 -> keeps name unless clash among live runs
    g2 = runs.give(st, eng, who["chris"], "login_fix_2", "sam@example.com")
    assert g2.owner == ids["sam"] and g2.name == "login_fix_2"
    with pytest.raises(NotFound):
        runs.get_run(st, who["chris"], "login_fix_2")
    ns = runs.notices(st, ids["pat"])
    assert ns == [{"id": ns[0]["id"], "task": sub_.id, "text": "given by sam to pat (pat@example.com)", "seen": 0}]
    kinds = [e["kind"] for e in eng.events(sub_.id)]
    assert kinds[-1] == "given"


def test_give_rename_suffix(env):
    st, eng, ids, who = env
    a = sub(eng, ids["pat"])
    b = sub(eng, ids["chris"])
    assert (a.name, b.name) == ("login_fix", "login_fix_2")
    sub(eng, ids["sam"], "Fix the login redirect")  # login_fix_3
    # free the base name then give: free_name against live runs of the same repo
    eng.claim()
    eng.transition(a.id, "cancelled")
    g = runs.give(st, eng, who["chris"], "login_fix_2", "pat@example.com")
    assert g.name == "login_fix_2" and g.owner == ids["pat"]


@pytest.mark.parametrize("target", ["pat", "nobody@example.com", "sam@example.com", "other@example.com", ""])
def test_give_refusals(env, target):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    real_members.lock(st, ids["sam"])
    with pytest.raises(runs.NoSuchMember):
        runs.give(st, eng, who["chris"], "login_fix", target)
    assert eng.get("w_ab12", t.id).owner == ids["chris"]
    assert runs.notices(st, ids["pat"]) == []


def test_give_needs_access(env):
    st, eng, ids, who = env
    sub(eng, ids["chris"])
    with pytest.raises(NotFound):
        runs.give(st, eng, who["pat"], "chris/login_fix", "sam@example.com")


def test_give_one_transaction(env):
    st, eng, ids, who = env
    sub(eng, ids["pat"])
    t = sub(eng, ids["chris"])
    before = (st.query("SELECT owner,name,updated FROM tasks WHERE id=?", (t.id,)),
              st.query("SELECT * FROM events"))
    st.exec("DROP TABLE notices")  # make the last statement fail
    with pytest.raises(Exception):
        runs.give(st, eng, who["chris"], "login_fix_2", "sam@example.com")
    after = (st.query("SELECT owner,name,updated FROM tasks WHERE id=?", (t.id,)),
             st.query("SELECT * FROM events"))
    assert after == before


def test_mark_seen(env):
    st, eng, ids, who = env
    sub(eng, ids["chris"])
    runs.give(st, eng, who["chris"], "login_fix", "pat@example.com")
    sub(eng, ids["chris"], "Add dark mode")
    runs.give(st, eng, who["chris"], "dark_add", "pat@example.com")
    assert [n["seen"] for n in runs.notices(st, ids["pat"])] == [0, 0]
    runs.mark_seen(st, ids["pat"])
    assert [n["seen"] for n in runs.notices(st, ids["pat"])] == [1, 1]
    assert runs.notices(st, ids["sam"]) == []


def test_mark_seen_run_only_that_run(env):
    st, eng, ids, who = env
    a = sub(eng, ids["chris"])
    b = sub(eng, ids["chris"], "Add dark mode")
    runs.give(st, eng, who["chris"], a.id, "pat@example.com")
    runs.give(st, eng, who["chris"], b.id, "pat@example.com")
    runs.mark_seen_run(st, ids["pat"], a.id)
    assert [(n["task"], n["seen"]) for n in runs.notices(st, ids["pat"])] == [(a.id, 1), (b.id, 0)]


def test_me(env):
    st, eng, ids, who = env
    st.exec("INSERT INTO workspaces(id,name,created) VALUES('w_ab12','acme','c')")
    assert runs.me(st, who["chris"]) == {"handle": "chris", "email": "chris@example.com",
                                         "role": "member", "workspace": "acme"}
    assert runs.me(st, who["root"])["role"] == "admin"
    st.exec("UPDATE members SET email=NULL WHERE id=?", (ids["pat"],))
    assert runs.me(st, who["pat"])["email"] is None


def test_live_runs(env):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    assert runs.live_runs(st, who["chris"]) == [
        {"name": "login_fix", "status": "queued", "step": None, "doing": None, "agent": "claude"}]
    eng.claim()
    eng.add_event(t.id, "tool", {"step": 13, "tool": "Read", "target": "a.py"})
    eng.add_event(t.id, "tool", {"step": 14, "tool": "Edit", "target": "api.py"})
    eng.add_event(t.id, "status", {"to": "running"})  # no step: skipped
    assert runs.live_runs(st, who["chris"]) == [
        {"name": "login_fix", "status": "running", "step": 14, "doing": "Edit api.py", "agent": "claude"}]
    assert runs.live_runs(st, who["pat"]) == []
    eng.transition(t.id, "done")
    assert runs.live_runs(st, who["chris"]) == []


def test_given_event_carries_handle_and_email(env):
    st, eng, ids, who = env
    t = sub(eng, ids["sam"], "Dark mode for settings")
    runs.give(st, eng, who["sam"], "dark_mode", "PAT@example.com")
    ev = [e for e in eng.events(t.id) if e["kind"] == "given"][-1]["data"]
    assert ev == {"from": ids["sam"], "to": "pat", "to_email": "pat@example.com", "name": "dark_mode"}


def test_live_runs_ignores_rename_and_result_events(env):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    eng.add_event(t.id, "tool", {"step": 3, "tool": "Edit", "target": "a.py"})
    eng.add_event(t.id, "rename", {"from": "login_fix", "to": "login_fix_2", "why": "branch taken on origin"})
    eng.add_event(t.id, "result", {"text": "done"})
    row = [r for r in runs.live_runs(st, who["chris"]) if r["name"] == t.name][0]
    assert row["step"] == 3 and row["doing"] == "Edit a.py"


def test_live_runs_strips_sandbox_prefix(env):
    st, eng, ids, who = env
    t = sub(eng, ids["chris"])
    eng.add_event(t.id, "tool", {"step": 2, "tool": "Edit", "target": "/work/api2.py"})
    row = [r for r in runs.live_runs(st, who["chris"]) if r["name"] == t.name][0]
    assert row["doing"] == "Edit api2.py"
