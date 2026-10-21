#!/usr/bin/env python3
"""Einer Paperless-Gruppe Anzeigerechte auf alle Dokumenttypen, Korrespondenten und Tags geben.

Bestehende Rechte und Eigentümer bleiben erhalten (merge). Nur Python-Standardbibliothek.
Mit dem Token eines Admins ausführen (der Companion-Token darf keine Rechte ändern).

    python3 paperless_grant_view.py --url http://paperless:8000 --token ADMIN_TOKEN --group ReadOnly
    python3 paperless_grant_view.py ... --dry-run     # nur anzeigen, nichts ändern
    python3 paperless_grant_view.py ... --documents   # zusätzlich alle Dokumente freigeben
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

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


def all_ids(base: str, token: str, endpoint: str) -> list[int]:
    ids, page = [], 1
    while True:
        data = request(base, token, f"/api/{endpoint}/?" + urllib.parse.urlencode({"page": page, "page_size": 100}))
        ids += [item["id"] for item in data.get("results", [])]
        if not data.get("next"):
            return ids
        page += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Paperless-Adresse, z.B. http://192.168.1.10:8000")
    parser.add_argument("--token", required=True, help="API-Token eines Admins")
    parser.add_argument("--group", required=True, help="Name der Gruppe, z.B. ReadOnly")
    parser.add_argument("--documents", action="store_true", help="auch alle Dokumente freigeben")
    parser.add_argument("--dry-run", action="store_true", help="nur zählen, nichts ändern")
    args = parser.parse_args()

    groups = request(args.url, args.token, "/api/groups/?" + urllib.parse.urlencode({"name__iexact": args.group}))
    match = [g for g in groups.get("results", []) if g["name"].lower() == args.group.lower()]
    if not match:
        sys.exit(f"Gruppe „{args.group}“ nicht gefunden")
    group_id = match[0]["id"]
    permissions = {"view": {"users": [], "groups": [group_id]}, "change": {"users": [], "groups": []}}

    for endpoint, label in OBJECT_TYPES.items():
        ids = all_ids(args.url, args.token, endpoint)
        print(f"{label}: {len(ids)}", end="")
        if ids and not args.dry_run:
            request(args.url, args.token, "/api/bulk_edit_objects/", {
                "objects": ids, "object_type": endpoint, "operation": "set_permissions",
                "permissions": permissions, "merge": True,
            })
            print(" – freigegeben", end="")
        print()

    if args.documents:
        ids = all_ids(args.url, args.token, "documents")
        print(f"Dokumente: {len(ids)}", end="")
        if ids and not args.dry_run:
            for start in range(0, len(ids), 500):
                request(args.url, args.token, "/api/documents/bulk_edit/", {
                    "documents": ids[start:start + 500], "method": "set_permissions",
                    "parameters": {"set_permissions": permissions, "merge": True},
                })
            print(" – freigegeben (Paperless verarbeitet das im Hintergrund)", end="")
        print()
    print("Fertig." if not args.dry_run else "Probelauf – nichts geändert.")


if __name__ == "__main__":
    main()
