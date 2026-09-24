"""The monitors after moving onto the shared client: same alerts, same
thresholds, same heartbeat stats. Network and disk are faked."""
import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import python_client  # noqa: E402  (the vendored copy the scripts fall back to)
import sc_paths  # noqa: E402


class Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.text = json.dumps(body or {"success": True})
        self._body = body or {"success": True}

    def json(self):
        return self._body


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(dict(kw, url=url))
        return Resp(204 if "webhooks" in url else 200)

    monkeypatch.setattr(python_client.requests, "post", fake_post)
    return calls


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_WEB_ROOT", str(tmp_path))
    monkeypatch.setenv("SC_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(sc_paths, "_root_cache", None)
    monkeypatch.setattr(sc_paths, "_env_cache", None)
    for key in ("DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID",
                "MAILJET_API_KEY", "MAILJET_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def fake_disk(monkeypatch, free, total=1000, used=None):
    used = total - free if used is None else used
    monkeypatch.setattr(python_client, "disk_usage", lambda p="/": {
        "path": p, "total": total, "used": used, "free": free,
        "free_percent": free / total * 100,
        "used_percent": used / (used + free) * 100})


def test_checkdisk_mails_below_30_percent_free(web, posts, monkeypatch):
    fake_disk(monkeypatch, free=250)
    monkeypatch.setenv("MAILJET_API_KEY", "k")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "s")
    runpy.run_path(str(ROOT / "check_disk.py"))
    mail = [c for c in posts if "mailjet" in c["url"]]
    assert len(mail) == 1
    assert mail[0]["json"]["Messages"][0]["Subject"] == "Disk Space Alert"
    assert mail[0]["json"]["Messages"][0]["TextPart"] == (
        "Warning: Disk space is below 30%. Current free space: 25.00%.")
    beat = [c for c in posts if c["url"].endswith("action=heartbeat")][-1]
    assert beat["json"]["metadata"]["alert_sent"] is True
    assert json.loads((web / "diskCheck.json").read_text())["free_percent"] == 25.0


def test_checkdisk_no_mail_above_30_percent(web, posts, monkeypatch):
    fake_disk(monkeypatch, free=400)
    monkeypatch.setenv("MAILJET_API_KEY", "k")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "s")
    runpy.run_path(str(ROOT / "check_disk.py"))
    assert not [c for c in posts if "mailjet" in c["url"]]


@pytest.fixture
def server_monitor(web, posts, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    sys.modules.pop("server_monitor", None)
    import server_monitor
    server_monitor._last_sent.clear()
    return server_monitor


def test_server_monitor_low_disk_alert(server_monitor, posts, monkeypatch):
    fake_disk(monkeypatch, free=50)
    monkeypatch.setattr(server_monitor, "disk_usage", python_client.disk_usage)
    assert server_monitor.check_disk_usage() is False
    (call,) = posts
    embed = call["json"]["embeds"][0]
    assert call["url"] == "https://discord.com/api/webhooks/1/x"
    assert embed["title"] == "❌ Low Disk Space"
    assert "Free space is **5.0%** (below 10% threshold)" in embed["description"]
    assert embed["footer"]["text"].startswith("Server Monitor | ")
    assert server_monitor.check_disk_usage() is False  # cooldown: not re-sent
    assert len(posts) == 1


def test_server_monitor_healthy_disk_sends_nothing(server_monitor, posts, monkeypatch):
    fake_disk(monkeypatch, free=500)
    monkeypatch.setattr(server_monitor, "disk_usage", python_client.disk_usage)
    assert server_monitor.check_disk_usage() is True
    assert posts == []


def test_server_monitor_unmounted_is_a_mount_error(server_monitor, posts, tmp_path):
    server_monitor.MOUNT_POINT = str(tmp_path)
    assert server_monitor.check_rclone_mount() is False
    assert posts[0]["json"]["embeds"][0]["title"] == "❌ Rclone Mount Error"


def test_server_monitor_never_mails(server_monitor, posts, monkeypatch):
    monkeypatch.setenv("MAILJET_API_KEY", "k")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "s")
    server_monitor.send_alert("X", "y")
    assert [c["url"] for c in posts] == ["https://discord.com/api/webhooks/1/x"]


def test_rclone_monitor_mount_check_is_the_shared_one():
    import rclone_monitor
    assert rclone_monitor.test_mount_accessible is python_client.mount_responds


def test_vendored_client_carries_the_shared_helpers():
    """python_client.py is a byte copy of the package client.py (1.1.0+)."""
    text = (ROOT / "python_client.py").read_text()
    assert "def send_alert(" in text and "def disk_usage(" in text


def test_old_checkdisk_name_runs_check_disk(web, posts, monkeypatch):
    """checkDisk.py is a stub kept for callers that still start the old path (#51)."""
    fake_disk(monkeypatch, free=250)
    runpy.run_path(str(ROOT / "checkDisk.py"))
    assert json.loads((web / "diskCheck.json").read_text())["free_percent"] == 25.0
