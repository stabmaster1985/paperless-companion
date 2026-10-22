"""Bestehende Regeln in Paperless lesen (nur lesend): Workflows und Zuordnungen an Tags/Korrespondenten/Typen.

Damit zeigt die Regel-Seite, welche Vorschläge in Paperless schon umgesetzt sind, wo sich etwas
widerspricht und welche Einträge noch auf „automatisch (lernend)“ stehen.
"""

import re

from app.identifiers import regex_for
from app.models import RuleProposal
from app.paperless import PaperlessClient, PaperlessError

ENDPOINTS = ("tags", "correspondents", "document_types")
ENDPOINT_LABELS = {"tags": "Tag", "correspondents": "Korrespondent", "document_types": "Dokumenttyp"}
MATCHING = {0: "keine", 1: "irgendein Wort", 2: "alle Wörter", 3: "exakt", 4: "regulärer Ausdruck", 5: "ungefähr",
            6: "automatisch (lernend)"}


def load(paperless: PaperlessClient) -> dict:
    """Workflows und Objekte mit Zuordnung lesen. Fehlende Rechte führen zu einem Hinweis, nicht zu einem Fehler."""
    data: dict = {"objects": {}, "workflows": [], "errors": []}
    for endpoint in ENDPOINTS:
        try:
            data["objects"][endpoint] = paperless.objects(endpoint)
        except PaperlessError as exc:
            data["objects"][endpoint] = []
            data["errors"].append(f"{ENDPOINT_LABELS[endpoint]}: {exc}")
    try:
        data["workflows"] = paperless.objects("workflows")
    except PaperlessError as exc:
        data["errors"].append(f"Workflows: {exc} – dem Companion-Benutzer fehlt evtl. das Recht „Workflow anzeigen“")
    return data


def _names(data: dict) -> dict[str, dict[int, str]]:
    return {e: {o["id"]: o["name"] for o in data["objects"].get(e, [])} for e in ENDPOINTS}


def workflows(data: dict) -> list[dict]:
    """Workflows vereinfacht: Bedingungen und Zuweisungen mit Namen."""
    names = _names(data)
    result = []
    for wf in data["workflows"]:
        conditions, tags, doctype, corr = [], [], None, None
        for t in wf.get("triggers") or []:
            if t.get("filter_has_correspondent"):
                conditions.append(("Korrespondent", names["correspondents"].get(t["filter_has_correspondent"], "?")))
            if t.get("filter_has_document_type"):
                conditions.append(("Dokumenttyp", names["document_types"].get(t["filter_has_document_type"], "?")))
            for tag_id in t.get("filter_has_tags") or []:
                conditions.append(("Tag", names["tags"].get(tag_id, "?")))
            if t.get("filter_filename"):
                conditions.append(("Dateiname", t["filter_filename"]))
        for a in wf.get("actions") or []:
            tags += [names["tags"].get(i, f"#{i}") for i in a.get("assign_tags") or []]
            doctype = names["document_types"].get(a.get("assign_document_type")) or doctype
            corr = names["correspondents"].get(a.get("assign_correspondent")) or corr
        result.append({"name": wf.get("name", ""), "enabled": wf.get("enabled", True), "conditions": conditions,
                       "tags": tags, "document_type": doctype, "correspondent": corr})
    return result


def matchings(data: dict) -> list[dict]:
    """Einträge mit Zuordnung (ohne „keine“), automatisch lernende zuerst."""
    rows = []
    for endpoint in ENDPOINTS:
        for o in data["objects"].get(endpoint, []):
            algo = o.get("matching_algorithm", 0)
            if algo:
                rows.append({"kind": ENDPOINT_LABELS[endpoint], "name": o["name"], "algorithm": algo,
                             "algorithm_label": MATCHING.get(algo, str(algo)), "match": o.get("match") or ""})
    return sorted(rows, key=lambda r: (r["algorithm"] != 6, r["kind"], r["name"].lower()))


def coverage(proposal: RuleProposal, data: dict) -> dict | None:
    """Ist der Vorschlag in Paperless schon umgesetzt? {"state": covered|partial|conflict, "note": …}"""
    trigger = proposal.trigger or "correspondent"
    condition = proposal.condition or proposal.correspondent
    objects = {e: {o["name"].lower(): o for o in data["objects"].get(e, [])} for e in ENDPOINTS}
    if trigger == "content":
        corr = objects["correspondents"].get(condition.lower())
        if not corr or not corr.get("matching_algorithm"):
            return None
        match, algo = corr.get("match") or "", corr["matching_algorithm"]
        terms = proposal.match_terms or []
        found = [t for t in terms if t.split(":", 1)[1].lower() in match.lower() or regex_for(*t.split(":", 1)) in match]
        if found and len(found) == len(terms):
            return {"state": "covered", "note": "Zuordnung am Korrespondenten enthält alle Merkmale"}
        if found:
            return {"state": "partial", "note": f"Zuordnung enthält {len(found)} von {len(terms)} Merkmalen"}
        return {"state": "partial", "note": f"Korrespondent hat schon eine Zuordnung ({MATCHING.get(algo, algo)}"
                                            f"{': ' + match if match else ''}) – ohne diese Merkmale"}
    if trigger == "keyword":
        word = condition.lower()
        names = [*proposal.tags, *([proposal.document_type] if proposal.document_type else [])]
        hits = 0
        for name in names:
            obj = objects["tags"].get(name.lower()) or objects["document_types"].get(name.lower())
            if obj and obj.get("matching_algorithm") in (1, 2, 3, 4, 5) and word in (obj.get("match") or "").lower():
                hits += 1
        if not hits:
            return None
        return {"state": "covered" if hits == len(names) else "partial", "note": f"Zuordnung „{condition}“ an {hits} von {len(names)}"}
    # Workflows mit Filter auf denselben Korrespondenten bzw. Dokumenttyp
    key = "Korrespondent" if trigger == "correspondent" else "Dokumenttyp"
    relevant = [w for w in workflows(data) if (key, condition) in w["conditions"] and w["enabled"]]
    if not relevant:
        return None
    tags = {t.lower() for w in relevant for t in w["tags"]}
    types = {w["document_type"] for w in relevant if w["document_type"]}
    if proposal.document_type and types and proposal.document_type not in types:
        return {"state": "conflict", "note": f"Workflow „{relevant[0]['name']}“ setzt Typ {', '.join(types)}"}
    missing = [t for t in proposal.tags if t.lower() not in tags]
    type_missing = proposal.document_type and proposal.document_type not in types
    if not missing and not type_missing:
        return {"state": "covered", "note": f"Workflow „{relevant[0]['name']}“"}
    return {"state": "partial", "note": f"Workflow „{relevant[0]['name']}“ – es fehlen: "
                                        + ", ".join(missing + ([f"Typ {proposal.document_type}"] if type_missing else []))}


def regex_ok(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False
