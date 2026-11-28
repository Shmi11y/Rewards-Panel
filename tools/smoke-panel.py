import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode


def request(path, *, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
    try:
        method = "POST" if body is not None else "GET"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def main():
    platform, token_path = sys.argv[1:3]
    check_persistence = "--check-persistence" in sys.argv
    for _ in range(60):
        try:
            token = Path(token_path).read_text().strip()
            if token and request("/")[0] == 403:
                break
        except OSError:
            pass
        time.sleep(.25)
    else:
        raise RuntimeError("Panel or token did not become ready")

    assert request("/api/status")[0] == 403
    assert request("/api/status", headers={"Cookie": f"rw_token={token}suffix"})[0] == 403
    assert request("/api/status", headers={"Cookie": f"rw_token={token}", "Host": "invalid.example"})[0] == 403
    code, headers, html = request("/?" + urlencode({"k": token}))
    assert code == 200 and b"Rewards" in html
    cookie = next(value for name, value in headers if name.lower() == "set-cookie").split(";")[0]

    def api(path, body=None):
        code, _, data = request(path, headers={"Cookie": cookie, "Content-Type": "application/json"}, body=body)
        assert code == 200, (path, code, data)
        return json.loads(data)

    status = api("/api/status")
    assert status["app"]["platform"] == platform
    assert status["app"]["nodeOk"] and status["app"]["entryOk"]
    assert status["app"]["browser"] != "未安装"
    expected_version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    assert api("/api/selftest")["portableVersion"] == expected_version
    if check_persistence:
        assert status["schedule"]["paused"] and status["schedule"]["mode"] == "daily"
        assert api("/api/prefs")["theme"] == "dark"
        for _ in range(44):
            if api("/api/schedule")["daemonAlive"]:
                break
            time.sleep(.25)
        else:
            raise RuntimeError("Restart did not recover the scheduler")
        return

    assert status["schedule"]["participation"] == "reminder"
    assert api("/api/config")["items"]["headless"] is True
    assert api("/api/prefs", {"theme": "dark"})["ok"]
    assert api("/api/participation", {"mode": "reminder"})["ok"]
    assert not api("/api/schedule", {"mode": "boot"})["ok"]
    assert api("/api/schedule", {"mode": "daily", "times": ["23:59"]})["daemonAlive"]
    assert api("/api/schedule/pause", {"paused": True})["paused"]
    assert api("/api/settings/backup")["backup"]["schedule"]["mode"] == "daily"


if __name__ == "__main__":
    main()
