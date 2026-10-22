"""Vorschläge für Paperless-Regeln aus deinem Archiv – und welche Dokumente davon abweichen.

Drei Muster werden gesucht (vorgemerkte Metadaten zählen schon mit):
- Korrespondent → Tags/Dokumenttyp („alle Stadtwerke-Dokumente: Tag Haus, Typ Rechnung“)
- Dokumenttyp → Tags („alle Lohnabrechnungen: Tag Gehalt“)
- Stichwort im Titel → Tags/Dokumenttyp („‚Versicherung‘ im Titel: Tag Versicherung“)

Zu jeder Regel zeigt die App die Ausreißer – Dokumente, die das Muster erfüllen, aber anders abgelegt
sind (vermutlich falsch einsortiert). Die lassen sich per Klick zum Korrigieren vormerken; geschrieben
wird erst über „Für Paperless“. Regeln selbst legt der Companion nie an: Er zeigt Anleitung und Inhalt.
"""

import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metadata import effective, rejected_tags
from app.models import Document, MetaDecision, RuleProposal
from app.pipeline import is_unresolved_tag

MIN_DOCS = 5
SHARE = 0.9  # Anteil für einen Regelvorschlag
KEYWORD_SHARE = 0.85
MAX_KEYWORDS = 25
YEAR_TAG = re.compile(r"(19|20)\d\d")
STATES = {"proposed": "Vorschlag", "approved": "Freigegeben", "created": "In Paperless angelegt", "dismissed": "Verworfen"}
# Reihenfolge = Reihenfolge der Anzeige: erst den Absender aus dem Text erkennen, dann folgt der Rest
TRIGGERS = {"content": "Korrespondent aus Text", "correspondent": "Korrespondent", "document_type": "Dokumenttyp",
            "keyword": "Stichwort im Titel"}
PREFIX = {"content": "text:", "correspondent": "", "document_type": "typ:", "keyword": "wort:"}
TEXT_MIN_DOCS = 3
TEXT_SHARE = 0.6
TERM_ORDER = {"iban": 0, "vat": 1, "domain": 2, "number": 3}
WORD = re.compile(r"[a-zäöüß][a-zäöüß\-]{3,}")
STOPWORDS = {
    "scan", "dokument", "dokumente", "seite", "seiten", "kopie", "datei", "neue", "neuer", "neues", "ihre", "ihren", "ihrer",
    "unser", "unsere", "über", "eine", "einer", "eines", "nach", "sowie", "oder", "und", "für", "vom", "zum", "zur",
    "januar", "februar", "märz", "april", "juni", "juli", "august", "september", "oktober", "november", "dezember",
    "jahr", "monat", "quartal", "teil", "anlage", "anlagen", "nummer", "kundennummer", "datum",
}


def _rule_tag(tag: str) -> bool:
    """Taugt der Tag für eine feste Regel? Jahres-Tags („2021“) hängen vom Datum ab, nicht vom Inhalt."""
    return not YEAR_TAG.fullmatch(tag) and not is_unresolved_tag(tag)


def title_words(title: str) -> set[str]:
    return {w.strip("-") for w in WORD.findall((title or "").lower()) if w.strip("-") not in STOPWORDS}


def key_of(trigger: str, condition: str) -> str:
    return PREFIX[trigger] + condition


def label(p: RuleProposal) -> str:
    trigger = p.trigger or "correspondent"
    if trigger == "content":
        return f"Korrespondent „{p.condition}“ am Text erkennen"
    return f"Wenn {TRIGGERS[trigger]} {'enthält' if trigger == 'keyword' else '='} „{p.condition or p.correspondent}“"


# --------------------------------------------------------------------------- Archiv lesen


def archive(session: Session) -> list[dict]:
    """Alle Dokumente mit wirksamem Stand (Paperless + Vorgemerktes)."""
    rows = session.execute(select(Document.id, Document.title, Document.tags, Document.correspondent,
                                  Document.document_type, Document.meta_accepted, Document.identifiers, Document.identifiers_at))
    result = []
    for doc_id, title, tags, corr, doctype, accepted, idents, scanned in rows:
        tags, corr, doctype = effective(tags, corr, doctype, accepted)
        title = (accepted or {}).get("title", {}).get("value", title)
        terms = {f"{kind}:{value}" for kind, values in (idents or {}).items() for value in values}
        result.append({"id": doc_id, "title": title or "", "tags": tags, "correspondent": corr, "document_type": doctype,
                       "terms": terms, "scanned": scanned is not None})
    return result


def matches(trigger: str, condition: str, doc: dict) -> bool:
    if trigger == "content":
        return doc["correspondent"] == condition
    if trigger == "correspondent":
        return doc["correspondent"] == condition
    if trigger == "document_type":
        return doc["document_type"] == condition
    return condition in title_words(doc["title"])


# --------------------------------------------------------------------------- Muster finden


def _pattern(rows: list[dict], share: float, exclude_tags: set[str] = frozenset(), with_type: bool = True) -> dict | None:
    n = len(rows)
    if n < MIN_DOCS:
        return None
    tag_counts = Counter(t for r in rows for t in set(r["tags"]))
    tags = {t: c for t, c in tag_counts.items() if c / n >= share and _rule_tag(t) and t.lower() not in exclude_tags}
    types = Counter(r["document_type"] for r in rows if r["document_type"]) if with_type else Counter()
    doctype = {d: c for d, c in types.items() if c / n >= share}
    if not tags and not doctype:
        return None
    return {"docs": n, "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])), "document_type": doctype}


def compute(session: Session, docs: list[dict] | None = None) -> dict[str, dict]:
    """Regel-Kandidaten: {Schlüssel: {"trigger", "condition", "docs", "tags": {...}, "document_type": {...}}}"""
    docs = docs if docs is not None else archive(session)
    result: dict[str, dict] = {}

    by_corr: dict[str, list[dict]] = {}
    by_type: dict[str, list[dict]] = {}
    by_word: dict[str, list[dict]] = {}
    for d in docs:
        if d["correspondent"]:
            by_corr.setdefault(d["correspondent"], []).append(d)
        if d["document_type"]:
            by_type.setdefault(d["document_type"], []).append(d)
        for w in title_words(d["title"]):
            by_word.setdefault(w, []).append(d)

    for corr, rows in by_corr.items():
        if found := _pattern(rows, SHARE, rejected_tags(session, corr)):
            result[key_of("correspondent", corr)] = {"trigger": "correspondent", "condition": corr, **found}
    for doctype, rows in by_type.items():
        found = _pattern(rows, SHARE, with_type=False)
        if found:
            result[key_of("document_type", doctype)] = {"trigger": "document_type", "condition": doctype, **found}

    # Stichworte: nur, wenn sie nicht ohnehin an einem Absender hängen (dann reicht die Absender-Regel)
    from app.identifiers import own_terms

    result.update(compute_text(docs, own_terms(session)))

    corr_names = {c.lower() for c in by_corr}
    keyword_rules = []
    for word, rows in by_word.items():
        if len(rows) < MIN_DOCS or word in corr_names or any(word in c for c in corr_names):
            continue
        top_corr = Counter(r["correspondent"] for r in rows).most_common(1)[0]
        if top_corr[0] and top_corr[1] / len(rows) >= 0.8:
            continue
        if found := _pattern(rows, KEYWORD_SHARE):
            # Vorrang: Stichwort, das im Namen eines vorgeschlagenen Tags/Typs vorkommt („versicherung“ vor „beitrag“)
            named = any(word in name.lower() for name in [*found["tags"], *found["document_type"]])
            keyword_rules.append((len(rows), named, word, found, frozenset(r["id"] for r in rows)))
    seen: set[frozenset] = set()
    for _, _, word, found, ids in sorted(keyword_rules, key=lambda k: (-k[0], not k[1], k[2])):
        if ids in seen:
            continue  # gleiche Dokumente wie ein besseres Stichwort
        seen.add(ids)
        result[key_of("keyword", word)] = {"trigger": "keyword", "condition": word, **found}
        if len(seen) >= MAX_KEYWORDS:
            break
    return result


def compute_text(docs: list[dict], own: set[str] = frozenset()) -> dict[str, dict]:
    """Merkmale (IBAN, USt-IdNr., Domain, Kundennr.), die (fast) nur bei Dokumenten EINES Korrespondenten vorkommen.

    Eigene Merkmale (deine IBANs aus Firefly, von dir als „eigen“ markierte) werden nie verwendet.
    """
    scanned = [d for d in docs if d["scanned"]]
    owners: dict[str, Counter] = {}  # Merkmal -> Korrespondenten (None = ohne)
    for d in scanned:
        for term in d["terms"] - own:
            owners.setdefault(term, Counter())[d["correspondent"]] += 1
    per_corr: dict[str, list[dict]] = {}
    for d in scanned:
        if d["correspondent"]:
            per_corr.setdefault(d["correspondent"], []).append(d)
    result = {}
    for corr, rows in per_corr.items():
        n = len(rows)
        if n < TEXT_MIN_DOCS:
            continue
        candidates = []
        for term, by_owner in owners.items():
            own = by_owner.get(corr, 0)
            foreign = sum(c for owner, c in by_owner.items() if owner not in (corr, None))
            if own >= 2 and own / n >= TEXT_SHARE and foreign == 0:
                candidates.append((TERM_ORDER.get(term.split(":")[0], 9), -own, term))
        if not candidates:
            continue
        chosen = [t for _, _, t in sorted(candidates)[:4]]
        coverage = sum(1 for d in rows if d["terms"] & set(chosen))
        result[key_of("content", corr)] = {
            "trigger": "content", "condition": corr, "docs": n, "coverage": coverage,
            "terms": {t: owners[t][corr] for t in chosen}, "tags": {}, "document_type": {},
        }
    return result


def refresh(session: Session) -> int:
    """Vorschläge anlegen/aktualisieren. Freigegebene, angelegte, verworfene und bearbeitete bleiben, wie sie sind."""
    found = compute(session)
    existing = {p.correspondent: p for p in session.scalars(select(RuleProposal))}
    new = 0
    for key, basis in found.items():
        p = existing.get(key)
        info = {k: v for k, v in basis.items() if k not in ("trigger", "condition")}
        if p is None:
            p = RuleProposal(correspondent=key, trigger=basis["trigger"], condition=basis["condition"], state="proposed")
            session.add(p)
            new += 1
        elif p.state != "proposed" or p.edited:
            p.basis = info  # Grundlage aktuell halten, Inhalt nicht
            continue
        p.trigger, p.condition, p.basis = basis["trigger"], basis["condition"], info
        p.tags = list(basis["tags"])
        p.document_type = next(iter(basis["document_type"]), None) if basis["trigger"] != "document_type" else None
        p.match_terms = list(basis["terms"]) if basis["trigger"] == "content" else None
    for key, p in existing.items():
        if key not in found and p.state == "proposed" and not p.edited:
            session.delete(p)
    return new


# --------------------------------------------------------------------------- Auswirkung und Ausreißer


def preview(session: Session, proposal: RuleProposal, docs: list[dict] | None = None) -> dict:
    """Was bewirkt die Regel bei den bisherigen passenden Dokumenten? Ausreißer = vermutlich falsch einsortiert."""
    trigger, condition = proposal.trigger or "correspondent", proposal.condition or proposal.correspondent
    docs = docs if docs is not None else archive(session)
    if trigger == "content":
        return _preview_text(proposal, condition, docs)
    unchanged, changed, conflicts = 0, [], []
    for d in docs:
        if not matches(trigger, condition, d):
            continue
        rejected = rejected_tags(session, d["correspondent"])
        have = {t.lower() for t in d["tags"]}
        add = [t for t in proposal.tags if t.lower() not in have]
        type_change = bool(proposal.document_type) and (d["document_type"] or "") != proposal.document_type
        if not add and not type_change:
            unchanged += 1
            continue
        entry = {"id": d["id"], "title": d["title"], "add": add, "correspondent": d["correspondent"],
                 "type_from": d["document_type"] if type_change else None,
                 "type_to": proposal.document_type if type_change else None}
        clash = any(t.lower() in rejected for t in add) or (type_change and d["document_type"])
        (conflicts if clash else changed).append(entry)
    return {"unchanged": unchanged, "changed": changed, "conflicts": conflicts,
            "total": unchanged + len(changed) + len(conflicts)}


def _preview_text(proposal: RuleProposal, corr: str, docs: list[dict]) -> dict:
    """Korrespondent aus Text: Wie viele eigene Dokumente werden erkannt, welche ohne Korrespondent kämen dazu,
    und träfe die Regel fälschlich Dokumente anderer Absender?"""
    terms = set(proposal.match_terms or [])
    unchanged, missed, changed, conflicts, scanned_own = 0, [], [], [], 0
    for d in docs:
        if not d["scanned"]:
            continue
        hit = bool(d["terms"] & terms)
        entry = {"id": d["id"], "title": d["title"], "add": [], "correspondent": d["correspondent"],
                 "set_corr": corr, "type_from": None, "type_to": None, "terms": sorted(d["terms"] & terms)}
        if d["correspondent"] == corr:
            scanned_own += 1
            if hit:
                unchanged += 1
            else:
                missed.append(entry)
        elif hit and not d["correspondent"]:
            changed.append(entry)
        elif hit:
            conflicts.append(entry)
    return {"unchanged": unchanged, "changed": changed, "conflicts": conflicts, "missed": missed,
            "own": scanned_own, "total": unchanged + len(missed) + len(changed) + len(conflicts)}


def prefill_fixes(session: Session, proposal: RuleProposal, doc_ids: list[int]) -> int:
    """Für ausgewählte Ausreißer die fehlenden Tags/den Typ VORMERKEN (nur im Companion, Quelle „Regel“).

    Übertragen wird wie alle Metadaten unter „Für Paperless“ – erst nach deiner Auswahl.
    """
    pv = preview(session, proposal)
    wanted = {e["id"]: e for e in pv["changed"] + pv["conflicts"] if e["id"] in doc_ids}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = 0
    for doc_id, e in wanted.items():
        doc = session.get(Document, doc_id)
        if doc is None:
            continue
        accepted = dict(doc.meta_accepted or {})
        if e.get("set_corr"):
            accepted["correspondent"] = {"value": e["set_corr"], "source": "rule", "new_tag": False, "at": stamp}
            _decide(session, doc, "correspondent", "correspondent", e["set_corr"])
        for tag in e["add"]:
            accepted[f"tag:{tag}"] = {"value": tag, "source": "rule", "new_tag": False, "at": stamp}
            _decide(session, doc, f"tag:{tag}", "tag", tag)
        if e["type_to"]:
            accepted["document_type"] = {"value": e["type_to"], "source": "rule", "new_tag": False, "at": stamp}
            _decide(session, doc, "document_type", "document_type", e["type_to"])
        doc.meta_accepted = accepted or None
        doc.meta_pending = True
        count += 1
    return count


def _decide(session: Session, doc: Document, key: str, item: str, value: str) -> None:
    row = session.scalar(select(MetaDecision).where(MetaDecision.document_id == doc.id, MetaDecision.key == key))
    if row is None:
        row = MetaDecision(document_id=doc.id, key=key, item=item)
        session.add(row)
    row.correspondent, row.suggested, row.value, row.source, row.decision = doc.correspondent, value, value, "rule", "accepted"
    row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.transferred_at = None


# --------------------------------------------------------------------------- ähnliche Namen


def _norm(name: str) -> str:
    n = re.sub(r"\b(gmbh|ag|se|kg|ohg|ug|mbh|co|e\.?\s?v|deutschland|germany)\b", " ", name.lower())
    n = re.sub(r"[^a-zäöüß0-9]", "", n)
    return re.sub(r"(en|e|s|n)$", "", n) if len(n) > 4 else n


def similar_names(session: Session) -> dict[str, list[list[tuple[str, int]]]]:
    """Ähnlich geschriebene Tags, Korrespondenten und Dokumenttypen (vermutlich doppelt angelegt)."""
    counts = {"Tags": Counter(), "Korrespondenten": Counter(), "Dokumenttypen": Counter()}
    for tags, corr, doctype in session.execute(select(Document.tags, Document.correspondent, Document.document_type)):
        counts["Tags"].update(t for t in tags or [] if not is_unresolved_tag(t))
        if corr:
            counts["Korrespondenten"][corr] += 1
        if doctype:
            counts["Dokumenttypen"][doctype] += 1
    result = {}
    for kind, counter in counts.items():
        groups: dict[str, list[tuple[str, int]]] = {}
        for name, n in counter.items():
            if (key := _norm(name)):
                groups.setdefault(key, []).append((name, n))
        found = [sorted(g, key=lambda x: -x[1]) for g in groups.values() if len(g) > 1]
        if found:
            result[kind] = sorted(found, key=lambda g: -sum(n for _, n in g))
    return result


# --------------------------------------------------------------------------- bearbeiten und anlegen


def update(proposal: RuleProposal, tags: list[str], document_type: str | None, terms: list[str] | None = None) -> None:
    if (proposal.trigger or "") == "content":
        proposal.match_terms = list(dict.fromkeys(t for t in terms or [] if t))
    proposal.tags = list(dict.fromkeys(t for t in tags if t))
    proposal.document_type = document_type or None
    proposal.edited = True
    proposal.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def set_state(proposal: RuleProposal, state: str) -> None:
    if state not in STATES:
        raise ValueError(state)
    proposal.state = state
    proposal.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def text_regex(terms: list[str]) -> str:
    """Regulärer Ausdruck für die Paperless-Zuordnung am Korrespondenten (findet IBAN auch mit Leerzeichen)."""
    from app.identifiers import regex_for

    return "|".join(regex_for(*t.split(":", 1)) for t in terms)


def api_payload(proposal: RuleProposal, ids: dict[str, dict[str, int]], objects: dict | None = None,
                review_tag: str = "") -> dict | list:
    """Inhalt für die Paperless-API – nur zur Kontrolle bzw. zum späteren Anlegen. Das Schema kann je nach Version abweichen.

    Korrespondent/Dokumenttyp: Workflow (Auslöser 2 = „Dokument hinzugefügt“, Aktion 1 = „Zuweisung“).
    Stichwort: Zuordnung am Tag/Dokumenttyp selbst (matching_algorithm 1 = „Irgendein Wort“).
    """
    trigger = proposal.trigger or "correspondent"
    condition = proposal.condition or proposal.correspondent
    if trigger == "content":
        return {"PATCH": f"/api/correspondents/{ids['correspondents'].get(condition.lower())}/",
                "match": text_regex(proposal.match_terms or []), "matching_algorithm": 4, "is_insensitive": True}
    if trigger == "keyword":
        changes = []
        for endpoint, names in (("tags", proposal.tags), ("document_types", [proposal.document_type] if proposal.document_type else [])):
            for name in names:
                obj_id = ids[endpoint].get(name.lower())
                current = ((objects or {}).get(endpoint) or {}).get(name.lower(), {})
                words = " ".join(dict.fromkeys([*(current.get("match") or "").split(), condition])) \
                    if current.get("matching_algorithm") == 1 else condition
                changes.append({"PATCH": f"/api/{endpoint}/{obj_id}/", "match": words, "matching_algorithm": 1})
        return changes
    tags = [*proposal.tags, *([review_tag] if review_tag and review_tag not in proposal.tags else [])]
    action: dict = {"type": 1, "assign_tags": [ids["tags"][t.lower()] for t in tags if t.lower() in ids["tags"]]}
    if proposal.document_type and proposal.document_type.lower() in ids["document_types"]:
        action["assign_document_type"] = ids["document_types"][proposal.document_type.lower()]
    filter_key = "filter_has_correspondent" if trigger == "correspondent" else "filter_has_document_type"
    filter_ids = ids["correspondents"] if trigger == "correspondent" else ids["document_types"]
    return {
        "name": f"Companion: {condition}",
        "order": 100,
        "enabled": True,
        "triggers": [{"type": 2, filter_key: filter_ids.get(condition.lower())}],
        "actions": [action],
    }


MATCHING = {0: "keine", 1: "irgendein Wort", 2: "alle Wörter", 3: "exakt", 4: "regulärer Ausdruck", 5: "ungefähr", 6: "automatisch (lernend)"}
