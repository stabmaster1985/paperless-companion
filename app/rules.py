"""Vorschläge für Paperless-Regeln (Workflows) aus deinem Archiv und deinen Entscheidungen.

Beispiel: Fast alle Dokumente der Stadtwerke tragen „Strom“ und „Haus“ und den Typ „Rechnung“ →
Vorschlag „Wenn Korrespondent = Stadtwerke, dann Tags Strom + Haus und Typ Rechnung setzen“.

Der Companion legt nichts in Paperless an. Er zeigt den Vorschlag, was er an deinen bisherigen
Dokumenten bewirkt hätte, und – nach deiner Freigabe – die Anleitung bzw. den genauen Inhalt zum Anlegen.
Vorgemerkte Metadaten zählen dabei schon mit, verworfene Tags werden nicht vorgeschlagen.
"""

import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metadata import _archive, rejected_tags
from app.models import Document, RuleProposal
from app.pipeline import is_unresolved_tag

MIN_DOCS = 5
SHARE = 0.9
YEAR_TAG = re.compile(r"(19|20)\d\d")
STATES = {"proposed": "Vorschlag", "approved": "Freigegeben", "created": "In Paperless angelegt", "dismissed": "Verworfen"}


def _rule_tag(tag: str) -> bool:
    """Taugt der Tag für eine feste Regel? Jahres-Tags („2021“) hängen vom Datum ab, nicht vom Absender."""
    return not YEAR_TAG.fullmatch(tag) and not is_unresolved_tag(tag)


def compute(session: Session) -> dict[str, dict]:
    """Regel-Kandidaten je Korrespondent: {"Stadtwerke": {"docs": 12, "tags": {...}, "document_type": {...}}}"""
    groups: dict[str, list[tuple[list[str], str | None]]] = {}
    for _, tags, corr, doctype in _archive(session):
        if corr:
            groups.setdefault(corr, []).append((tags, doctype))
    result = {}
    for corr, rows in groups.items():
        n = len(rows)
        if n < MIN_DOCS:
            continue
        rejected = rejected_tags(session, corr)
        tag_counts = Counter(t for tags, _ in rows for t in set(tags))
        types = Counter(d for _, d in rows if d)
        tags = {t: c for t, c in tag_counts.items() if c / n >= SHARE and _rule_tag(t) and t.lower() not in rejected}
        doctype = {d: c for d, c in types.items() if c / n >= SHARE}
        if tags or doctype:
            result[corr] = {"docs": n, "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
                            "document_type": doctype}
    return result


def refresh(session: Session) -> int:
    """Vorschläge anlegen/aktualisieren. Freigegebene, angelegte, verworfene und bearbeitete bleiben, wie sie sind."""
    found = compute(session)
    existing = {p.correspondent: p for p in session.scalars(select(RuleProposal))}
    new = 0
    for corr, basis in found.items():
        p = existing.get(corr)
        if p is None:
            p = RuleProposal(correspondent=corr, state="proposed")
            session.add(p)
            new += 1
        elif p.state != "proposed" or p.edited:
            p.basis = basis  # Grundlage aktuell halten, Inhalt nicht
            continue
        p.basis = basis
        p.tags = list(basis["tags"])
        p.document_type = next(iter(basis["document_type"]), None)
    # Vorschläge, deren Grundlage weggefallen ist (und die du nicht angefasst hast), entfernen
    for corr, p in existing.items():
        if corr not in found and p.state == "proposed" and not p.edited:
            session.delete(p)
    return new


def preview(session: Session, proposal: RuleProposal) -> dict:
    """Was hätte die Regel bei deinen bisherigen Dokumenten dieses Absenders bewirkt?"""
    rejected = rejected_tags(session, proposal.correspondent)
    unchanged, changed, conflicts = 0, [], []
    for doc in session.scalars(select(Document).where(Document.correspondent == proposal.correspondent).order_by(Document.id)):
        have = {t.lower() for t in doc.tags or []}
        accepted = doc.meta_accepted or {}
        have |= {k[4:].lower() for k in accepted if k.startswith("tag:")}
        add = [t for t in proposal.tags if t.lower() not in have]
        current_type = accepted.get("document_type", {}).get("value", doc.document_type)
        type_change = proposal.document_type and (current_type or "") != proposal.document_type
        clash = [t for t in add if t.lower() in rejected]
        if not add and not type_change:
            unchanged += 1
            continue
        entry = {"id": doc.id, "title": doc.title, "add": add, "type_from": current_type if type_change else None,
                 "type_to": proposal.document_type if type_change else None}
        (conflicts if clash or (type_change and current_type) else changed).append(entry)
    return {"unchanged": unchanged, "changed": changed, "conflicts": conflicts,
            "total": unchanged + len(changed) + len(conflicts)}


def update(proposal: RuleProposal, tags: list[str], document_type: str | None) -> None:
    proposal.tags = [t for t in tags if t]
    proposal.document_type = document_type or None
    proposal.edited = True
    proposal.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def set_state(proposal: RuleProposal, state: str) -> None:
    if state not in STATES:
        raise ValueError(state)
    proposal.state = state
    proposal.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def api_payload(proposal: RuleProposal, ids: dict[str, dict[str, int]]) -> dict:
    """Inhalt für die Paperless-API (POST /api/workflows/) – zur Kontrolle bzw. zum späteren Anlegen.

    Auslöser 2 = „Dokument hinzugefügt“, Aktion 1 = „Zuweisung“. Je nach Paperless-Version kann das Schema abweichen.
    """
    corr_id = ids["correspondents"].get(proposal.correspondent.lower())
    action: dict = {"type": 1, "assign_tags": [ids["tags"][t.lower()] for t in proposal.tags if t.lower() in ids["tags"]]}
    if proposal.document_type and proposal.document_type.lower() in ids["document_types"]:
        action["assign_document_type"] = ids["document_types"][proposal.document_type.lower()]
    return {
        "name": f"Companion: {proposal.correspondent}",
        "order": 100,
        "enabled": True,
        "triggers": [{"type": 2, "filter_has_correspondent": corr_id}],
        "actions": [action],
    }
