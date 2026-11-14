import http.client
import os
import subprocess
import sys
import threading

from slcloud.app import probe_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_probe_server_serves_and_shuts_down():
    srv = probe_server()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1" and port > 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/health")
    assert conn.getresponse().status == 200
    conn.close()
    srv.shutdown()
    t.join(10)
    assert not t.is_alive()
    srv.server_close()


def test_module_entry_unknown_command_exits_nonzero_without_traceback():
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    p = subprocess.run([sys.executable, "-m", "slcloud", "zz-no-such-command"], capture_output=True,
                       text=True, env=env, cwd=ROOT, timeout=30)
    assert p.returncode != 0
    assert "usage" in p.stderr and "Traceback" not in p.stderr


def _get_status(srv, path):
    host, port = srv.server_address[:2]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", path)
        status = conn.getresponse().status
        conn.close()
        return status
    finally:
        srv.shutdown()
        t.join(10)
        srv.server_close()


def test_probe_server_adds_token_for_authenticated_route():
    assert _get_status(probe_server(), "/api/tasks") != 401


def test_build_directly_still_requires_auth(tmp_path):
    import secrets

    from fastapi.testclient import TestClient

    from slcloud.app import build
    from slcloud.config import Settings

    s = Settings(data_dir=str(tmp_path), db_path=str(tmp_path / "d.db"), work_dir=str(tmp_path / "w"),
                 master_key=secrets.token_urlsafe(32), sandbox="process")
    assert TestClient(build(s)).get("/api/tasks").status_code == 401
