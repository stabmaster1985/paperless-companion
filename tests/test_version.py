import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.gitinfo import read_git_version
from app.main import create_app
from app.version import Version

OLD, NEW = "a" * 40, "b" * 40


def fake_repo(root, commit=OLD, packed=False):
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "logs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs\n{commit} refs/heads/main\n")
    else:
        (git / "refs" / "heads" / "main").write_text(commit + "\n")
    (git / "logs" / "HEAD").write_text(f"0000 {commit} Ich <ich@example.org> 1792000000 +0200\tpull: Fast-forward\n")
    return root


def test_read_git_version(tmp_path):
    info = read_git_version(fake_repo(tmp_path / "a"))
    assert info == {"commit": OLD, "branch": "main", "updated_at": "2026-10-14T17:46:40+00:00"}
    assert read_git_version(fake_repo(tmp_path / "b", packed=True))["commit"] == OLD
    assert read_git_version(tmp_path / "leer") == {}


def test_version_json_wins(tmp_path):
    fake_repo(tmp_path)
    (tmp_path / "version.json").write_text(json.dumps({"commit": NEW, "branch": "main"}))
    assert Version(tmp_path, "x/y").current()["commit"] == NEW


def github_mock(router, behind=2):
    router.get("https://api.github.com/repos/me/companion/commits/main").respond(
        json={"sha": NEW, "commit": {"message": "Neu\n\nDetails", "committer": {"date": "2026-10-20T10:00:00Z"}}})
    router.get(f"https://api.github.com/repos/me/companion/compare/{OLD}...{NEW}").respond(json={
        "status": "ahead", "ahead_by": behind,
        "commits": [{"sha": "c" * 40, "commit": {"message": "Erster", "committer": {"date": "2026-10-19T10:00:00Z"}}},
                    {"sha": NEW, "commit": {"message": "Neu", "committer": {"date": "2026-10-20T10:00:00Z"}}}],
    })


def test_check_reports_update_and_caches(tmp_path):
    fake_repo(tmp_path)
    with respx.mock as router:
        github_mock(router)
        v = Version(tmp_path, "me/companion", token="geheim")
        v.refresh()
        info = v.get()
        assert info["status"] == "update" and info["behind"] == 2 and v.update_available
        assert [c["message"] for c in info["commits"]] == ["Neu", "Erster"]
        assert router.calls[0].request.headers["authorization"] == "Bearer geheim"
        calls = len(router.calls)
        v.refresh()  # innerhalb einer Stunde keine neue Anfrage
        assert len(router.calls) == calls


def test_check_private_repo_error(tmp_path):
    fake_repo(tmp_path)
    with respx.mock as router:
        router.get("https://api.github.com/repos/me/companion/commits/main").respond(404)
        v = Version(tmp_path, "me/companion")
        v.refresh()
        assert "privat" in v.get()["error"] and not v.update_available


def test_current_when_equal(tmp_path):
    fake_repo(tmp_path, commit=NEW)
    with respx.mock as router:
        github_mock(router)
        v = Version(tmp_path, "me/companion")
        v.refresh()
        assert v.get()["status"] == "current"


def test_disabled_check_makes_no_requests(tmp_path):
    fake_repo(tmp_path)
    with respx.mock as router:
        v = Version(tmp_path, "me/companion", enabled=False)
        v.refresh(force=True)
        assert not router.calls and v.get()["enabled"] is False


@pytest.fixture
def client(settings, mock_api, tmp_path):
    settings.update_repo = "me/companion"
    settings.update_webhook_url = "http://n8n.test/webhook/paperless-companion-update"
    settings.update_webhook_secret = "s3cret"
    app = create_app(settings, start_worker=False)
    app.state.version = Version(fake_repo(tmp_path / "repo"), "me/companion")
    with TestClient(app) as c:
        yield c


def test_system_card_and_update_button(client, mock_api):
    github_mock(mock_api)
    client.post("/system/version-check")
    page = client.get("/system").text
    assert "Update verfügbar · 2 neue Commits" in page and "Jetzt aktualisieren" in page
    assert 'title="Update verfügbar"' in client.get("/").text

    hook = mock_api.post("http://n8n.test/webhook/paperless-companion-update").respond(202)
    result = client.post("/system/update").json()
    assert result["ok"] and result["von"] == OLD
    request = hook.calls.last.request
    assert request.headers["x-update-key"] == "s3cret"
    assert json.loads(request.content) == {"typ": "update", "von": OLD, "auf": NEW}
    assert client.get("/api/version").json()["current"]["commit"] == OLD


def test_update_webhook_failure(client, mock_api):
    mock_api.post("http://n8n.test/webhook/paperless-companion-update").mock(side_effect=httpx.ConnectError("weg"))
    assert client.post("/system/update").json() == {"ok": False, "error": "weg"}
