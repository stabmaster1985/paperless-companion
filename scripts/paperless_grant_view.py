#!/usr/bin/env python3
"""Paperless-Gruppen Anzeigerechte auf Dokumenttypen, Korrespondenten, Tags, Speicherpfade (und Dokumente) geben,
optional einen Eigentümer setzen.

Nur Python-Standardbibliothek. Mit dem Token eines Admins ausführen (der Companion-Token darf keine Rechte ändern).

    python3 paperless_grant_view.py --url http://paperless:8000 --token ADMIN_TOKEN --group ReadOnly
    python3 paperless_grant_view.py ... --group ReadOnly --group "Normaler Benutzer"   # mehrere Gruppen
    python3 paperless_grant_view.py ... --documents   # zusätzlich alle Dokumente freigeben
    python3 paperless_grant_view.py ... --owner admin --replace   # Eigentümer überall auf „admin“ setzen
    python3 paperless_grant_view.py ... --dry-run     # nur anzeigen, nichts ändern

Ohne --replace werden Rechte ergänzt (bestehende bleiben), und ein Eigentümer wird nur dort gesetzt, wo noch
keiner eingetragen ist – so verhält sich Paperless beim Zusammenführen. Mit --replace gelten danach genau die
angegebenen Rechte: Eigentümer = --owner, Anzeigen = --group, Bearbeiten = --change-group (sonst niemand).
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

OBJECT_TYPES = {"document_types": "Dokumenttypen", "correspondents": "Korrespondenten", "tags": "Tags",
                "storage_paths": "Speicherpfade"}


def request(base: str, token: str, path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Token {token}", "Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"Fehler {exc.code} bei {path}: {exc.read().decode(errors='replace')[:500]}")
    return json.loads(body) if body else {}


def all_items(base: str, token: str, endpoint: str, **extra) -> list[dict]:
    items, page = [], 1
    while True:
        params = {"page": page, "page_size": 100, **extra}
        data = request(base, token, f"/api/{endpoint}/?" + urllib.parse.urlencode(params))
        items += data.get("results", [])
        if not data.get("next"):
            return items
        page += 1


def find_id(base: str, token: str, endpoint: str, field: str, name: str, label: str) -> int:
    data = request(base, token, f"/api/{endpoint}/?" + urllib.parse.urlencode({f"{field}__iexact": name}))
    match = [x for x in data.get("results", []) if x[field].lower() == name.lower()]
    if not match:
        sys.exit(f"{label} „{name}“ nicht gefunden")
    return match[0]["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Paperless-Adresse, z.B. http://192.168.1.10:8000")
    parser.add_argument("--token", required=True, help="API-Token eines Admins")
    parser.add_argument("--group", required=True, action="append",
                        help="Gruppe mit Anzeigerecht, z.B. ReadOnly (mehrfach angeben für mehrere Gruppen)")
    parser.add_argument("--change-group", action="append", default=[],
                        help="Gruppe mit Bearbeitungsrecht (optional, mehrfach möglich)")
    parser.add_argument("--owner", help="Benutzername des Eigentümers, z.B. admin")
    parser.add_argument("--replace", action="store_true",
                        help="Rechte genau so setzen statt ergänzen (nötig, um einen vorhandenen Eigentümer zu ersetzen)")
    parser.add_argument("--documents", action="store_true", help="auch alle Dokumente freigeben")
    parser.add_argument("--dry-run", action="store_true", help="nur zählen, nichts ändern")
    args = parser.parse_args()

    users = {u["id"]: u["username"] for u in all_items(args.url, args.token, "users")}
    view = [find_id(args.url, args.token, "groups", "name", g, "Gruppe") for g in args.group]
    change = [find_id(args.url, args.token, "groups", "name", g, "Gruppe") for g in args.change_group]
    owner = find_id(args.url, args.token, "users", "username", args.owner, "Benutzer") if args.owner else None
    permissions = {"view": {"users": [], "groups": view}, "change": {"users": [], "groups": change}}
    merge = not args.replace

    print(f"Anzeigen: {', '.join(args.group)}"
          + (f" · Bearbeiten: {', '.join(args.change_group)}" if args.change_group else "")
          + (f" · Eigentümer: {args.owner}" if owner else "")
          + (" · ersetzen (genau diese Rechte)" if args.replace else " · ergänzen (bestehende Rechte bleiben)"))
    if owner and merge:
        print("Hinweis: Ohne --replace setzt Paperless den Eigentümer nur dort, wo noch keiner eingetragen ist.")

    def describe(items: list[dict]) -> str:
        owners = Counter(users.get(i.get("owner"), "ohne Eigentümer") if i.get("owner") else "ohne Eigentümer"
                         for i in items)
        return ", ".join(f"{n}× {name}" for name, n in owners.most_common())

    def payload_extra() -> dict:
        return {"owner": owner} if owner is not None else {}

    for endpoint, label in OBJECT_TYPES.items():
        items = all_items(args.url, args.token, endpoint)
        print(f"{label}: {len(items)}" + (f" (bisher: {describe(items)})" if items else ""), end="")
        if items and not args.dry_run:
            request(args.url, args.token, "/api/bulk_edit_objects/", {
                "objects": [i["id"] for i in items], "object_type": endpoint, "operation": "set_permissions",
                "permissions": permissions, "merge": merge, **payload_extra(),
            })
            print(" – erledigt", end="")
        print()

    if args.documents:
        docs = all_items(args.url, args.token, "documents", fields="id,owner", truncate_content="true")
        print(f"Dokumente: {len(docs)}" + (f" (bisher: {describe(docs)})" if docs else ""), end="")
        if docs and not args.dry_run:
            ids = [d["id"] for d in docs]
            for start in range(0, len(ids), 500):
                request(args.url, args.token, "/api/documents/bulk_edit/", {
                    "documents": ids[start:start + 500], "method": "set_permissions",
                    "parameters": {"set_permissions": permissions, "merge": merge, **payload_extra()},
                })
            print(" – erledigt (Paperless verarbeitet das im Hintergrund)", end="")
        print()
    print("Fertig." if not args.dry_run else "Probelauf – nichts geändert.")


if __name__ == "__main__":
    main()
