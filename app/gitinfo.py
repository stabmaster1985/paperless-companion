"""Git-Stand ohne git-Programm lesen (nur Standardbibliothek – läuft auch in der Docker-Build-Stufe)."""

import re
from datetime import datetime, timezone
from pathlib import Path


def read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_git_version(root: Path) -> dict:
    """Commit, Branch und Zeitpunkt des letzten Checkouts/Pulls – ohne git-Programm."""
    git = Path(root) / ".git"
    head = (read_file(git / "HEAD") or "").strip()
    if not head:
        return {}
    commit, branch = head, None
    if head.startswith("ref: "):
        ref = head[5:]
        branch = ref.removeprefix("refs/heads/")
        commit = (read_file(git / ref) or "").strip() or None
        if commit is None:
            for line in (read_file(git / "packed-refs") or "").splitlines():
                if line.endswith(f" {ref}"):
                    commit = line.split(" ")[0]
                    break
    updated_at = None
    log_lines = (read_file(git / "logs" / "HEAD") or "").strip().splitlines()
    if log_lines and (m := re.search(r"> (\d+) [+-]\d{4}\t", log_lines[-1])):
        updated_at = datetime.fromtimestamp(int(m.group(1)), timezone.utc).isoformat()
    return {"commit": commit, "branch": branch, "updated_at": updated_at}
