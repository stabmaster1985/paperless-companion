"""Version des Companion und Prüfung auf Updates (wie im firefly-dashboard).

Die laufende Version kommt aus version.json (beim Docker-Build erzeugt) oder direkt
aus dem .git-Verzeichnis (bei Start aus einem Klon). Ob es Neues gibt, fragt der
Hintergrundprozess höchstens einmal pro Stunde bei GitHub nach.
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.gitinfo import read_file, read_git_version

CHECK_INTERVAL = 3600


class Version:
    def __init__(self, root: Path, repo: str, enabled: bool = True, token: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.root = Path(root)
        self.repo = repo
        self.enabled = enabled and bool(repo)
        self.token = token
        self._transport = transport
        self._lock = threading.Lock()
        self._cached: dict = {}
        self._checked_at = 0.0

    def current(self) -> dict:
        try:
            data = json.loads(read_file(self.root / "version.json") or "null")
        except json.JSONDecodeError:
            data = None
        return data if data and data.get("commit") else read_git_version(self.root)

    def _github(self, path: str) -> dict:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "paperless-companion"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(timeout=10, transport=self._transport) as client:
            response = client.get(f"https://api.github.com/repos/{self.repo}/{path}", headers=headers)
        if response.status_code == 404:
            raise RuntimeError("Repository nicht gefunden oder privat (UPDATE_GITHUB_TOKEN setzen)")
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub: HTTP {response.status_code}")
        return response.json()

    def _check(self) -> dict:
        cur = self.current()
        latest = self._github(f"commits/{cur.get('branch') or 'main'}")
        commit_info = latest.get("commit") or {}
        info = {
            "latest": {
                "commit": latest.get("sha"),
                "date": (commit_info.get("committer") or {}).get("date"),
                "message": (commit_info.get("message") or "").split("\n")[0],
            },
            "behind": 0,
            "commits": [],
            # update: GitHub ist weiter · current: gleich oder lokal neuer · unknown: lokaler Stand unbekannt
            "status": "current" if cur.get("commit") == latest.get("sha") else "unknown",
        }
        if cur.get("commit") and cur["commit"] != latest.get("sha"):
            try:
                cmp = self._github(f"compare/{cur['commit']}...{latest['sha']}")
            except RuntimeError:
                info["behind"] = None  # Commit ist GitHub unbekannt (z.B. lokale Änderungen)
            else:
                info["behind"] = cmp.get("ahead_by", 0)
                info["status"] = "update" if cmp.get("status") in ("ahead", "diverged") else "current"
                info["commits"] = [
                    {
                        "commit": c.get("sha"),
                        "date": ((c.get("commit") or {}).get("committer") or {}).get("date"),
                        "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0],
                    }
                    for c in reversed(cmp.get("commits") or [])
                ][:20]
        return info

    def refresh(self, force: bool = False) -> None:
        """Bei GitHub nachfragen (höchstens stündlich, außer force)."""
        if not self.enabled:
            return
        with self._lock:
            if not force and self._checked_at and time.monotonic() - self._checked_at < CHECK_INTERVAL:
                return
            try:
                self._cached = {**self._check(), "error": None}
            except (RuntimeError, httpx.HTTPError) as exc:
                self._cached = {**self._cached, "error": str(exc)}
            self._checked_at = time.monotonic()
            self._cached["checked_at"] = datetime.now(timezone.utc).isoformat()

    def get(self) -> dict:
        """Stand für die Oberfläche (ohne GitHub-Anfrage)."""
        return {"current": self.current(), "repo": self.repo, "enabled": self.enabled, **self._cached}

    @property
    def update_available(self) -> bool:
        return self._cached.get("status") == "update" and not self._cached.get("error")
