"""Vorschläge für Paperless-Metadaten: Titel, Tags, Korrespondent, Dokumenttyp.

Ablauf: vorschlagen -> vormerken oder verwerfen (nur im Companion) -> gesammelt übertragen.
Nach Paperless geschrieben wird erst beim Übertragen (``transfer``).

Zwei Quellen:
- „Verlauf“: Was deine übrigen Dokumente desselben Korrespondenten tragen.
- „AI“: Das lokale Modell liest den Anfang des Dokuments und wählt aus deinen vorhandenen
  Tags, Korrespondenten und Dokumenttypen (per JSON-Schema auf diese Listen beschränkt).

Gelernt wird bereits aus Vorgemerktem: Vorgemerkte Tags/Typen/Korrespondenten zählen für den
Verlauf und das Vokabular so, als stünden sie schon in Paperless, vorgemerkte Titel dienen der AI
als Stilbeispiele, verworfene Tags werden für den Absender nicht mehr vorgeschlagen.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.extraction.rules import normalize
from app.models import Document, MetaDecision
from app.ollama import OllamaClient, OllamaError
from app.paperless import PaperlessClient

ITEMS = {"title": "Titel", "correspondent": "Korrespondent", "document_type": "Dokumenttyp", "tag": "Tag"}
SOURCES = {"llm": "AI", "history": "Verlauf"}
MAX_TAGS = 4
MAX_NEW_TAGS = 2
VOCAB_LIMIT = 200

# Typische Titel aus Scannern und Dateinamen: „Scan_0012“, „IMG 2024-01-03“, „20240103123011“, „Dokument (3)“
GENERIC_TITLE = re.compile(
    r"^(?:(?:scan|scanned|img|image|bild|foto|photo|dok|doc|document|dokument|datei|file|unbenannt|untitled|neu|new|"
    r"brother|canon|epson|hp|xerox|pdf)(?![a-zäöüß])[\s_\-.]*)?[\d\s_\-.:()]*$",
    re.IGNORECASE,
)


def is_generic_title(title: str | None, original_file_name: str | None = None) -> bool:
    """Nichtssagender Titel (leer, nur Zahlen/Datum, Scanner-Name oder = Dateiname)?"""
    title = (title or "").strip()
    if not title or GENERIC_TITLE.fullmatch(title):
        return True
    if original_file_name:
        stem = PurePath(original_file_name).stem.strip().lower()
        return title.lower() in (stem, original_file_name.strip().lower())
    return False


# --------------------------------------------------------------------------- Kontext aus dem Archiv


def effective(doc_tags, correspondent, document_type, accepted: dict | None) -> tuple[list[str], str | None, str | None]:
    """Werte inklusive vorgemerkter, noch nicht übertragener Vorschläge."""
    accepted = accepted or {}
    tags = list(doc_tags or [])
    have = {t.lower() for t in tags}
    for key, entry in accepted.items():
        if key.startswith("tag:") and entry["value"].lower() not in have:
            tags.append(entry["value"])
            have.add(entry["value"].lower())
    corr = accepted["correspondent"]["value"] if "correspondent" in accepted else correspondent
    doctype = accepted["document_type"]["value"] if "document_type" in accepted else document_type
    return tags, corr, doctype


def _archive(session: Session) -> list[tuple[int, list[str], str | None, str | None]]:
    rows = session.execute(select(Document.id, Document.tags, Document.correspondent, Document.document_type,
                                  Document.meta_accepted))
    return [(i, *effective(t, c, d, a)) for i, t, c, d, a in rows]


def vocabulary(session: Session, names: dict, archive: list | None = None) -> dict[str, list[str]]:
    """Vorhandene Tags/Korrespondenten/Dokumenttypen, die häufig benutzten zuerst.

    Vorgemerkte neue Tags gehören schon dazu, damit gleiche Dokumente denselben neuen Tag bekommen.
    """
    usage: dict[str, Counter] = {"tags": Counter(), "correspondents": Counter(), "document_types": Counter()}
    for _, tags, corr, doctype in archive if archive is not None else _archive(session):
        usage["tags"].update(tags)
        if corr:
            usage["correspondents"][corr] += 1
        if doctype:
            usage["document_types"][doctype] += 1
    result = {}
    for key in usage:
        available = list(names.get(key, {}).values())
        if key == "tags":
            known = {n.lower() for n in available}
            available += [t for t in usage["tags"] if t.lower() not in known]
        available.sort(key=lambda n: (-usage[key][n], n.lower()))
        result[key] = available[:VOCAB_LIMIT] if key != "document_types" else available
    return result


def rejected_tags(session: Session, correspondent: str | None) -> set[str]:
    """Für diesen Korrespondenten verworfene Tags (kleingeschrieben)."""
    if not correspondent:
        return set()
    return {v.lower() for v in session.scalars(select(MetaDecision.value).where(
        MetaDecision.item == "tag", MetaDecision.decision == "dismissed", MetaDecision.correspondent == correspondent))}


def history(session: Session, doc: Document, archive: list | None = None) -> dict:
    """Was tragen die anderen Dokumente desselben Korrespondenten (inkl. Vorgemerktem)? Ab 3 Dokumenten."""
    _, corr, _ = effective(doc.tags, doc.correspondent, doc.document_type, doc.meta_accepted)
    if not corr:
        return {"count": 0, "tags": [], "document_type": None}
    rows = [(t, d) for i, t, c, d in (archive if archive is not None else _archive(session)) if c == corr and i != doc.id]
    count = len(rows)
    tags = Counter(t for tag_list, _ in rows for t in set(tag_list))
    types = Counter(t for _, t in rows if t)
    common_tags = [t for t, n in tags.most_common() if count >= 3 and n / count >= 0.5]
    doctype = None
    if types and count >= 3:
        name, n = types.most_common(1)[0]
        doctype = name if n / count >= 0.6 else None
    return {"count": count, "tags": common_tags, "document_type": doctype}


def title_examples(session: Session, correspondent: str | None, limit: int = 4) -> list[str]:
    """Von dir vorgemerkte Titel (auch noch nicht übertragene) als Stilvorlage, gleicher Absender zuerst."""
    rows = session.execute(
        select(MetaDecision.value, MetaDecision.correspondent).where(
            MetaDecision.item == "title", MetaDecision.decision.in_(("accepted", "edited"))
        ).order_by(MetaDecision.decided_at.desc()).limit(50)
    ).all()
    rows.sort(key=lambda r: r[1] != correspondent)  # stabil: gleicher Absender zuerst, sonst neueste
    return [value for value, _ in rows[:limit] if value]


# --------------------------------------------------------------------------- AI

SYSTEM = (
    "Du ordnest deutsche Dokumente (OCR-Text, kann Fehler enthalten) in einem privaten Dokumentenarchiv ein. "
    "Wähle Tags, Korrespondent und Dokumenttyp nur aus den vorgegebenen Listen. "
    "Antworte ausschließlich mit JSON nach dem Schema."
)


def schema(vocab: dict[str, list[str]]) -> dict:
    def choice(values: list[str]) -> dict:
        return {"type": "string", "enum": ["", *values]} if values else {"type": "string"}

    tag_item = {"type": "string", "enum": vocab["tags"]} if vocab["tags"] else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tags": {"type": "array", "items": tag_item, "maxItems": MAX_TAGS},
            "new_tags": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_NEW_TAGS},
            "correspondent": choice(vocab["correspondents"]),
            "document_type": choice(vocab["document_types"]),
        },
        "required": ["title", "tags", "new_tags", "correspondent", "document_type"],
    }


def build_prompt(doc: Document, text: str, vocab: dict, hist: dict, rejected: set[str], max_input: int,
                 examples: list[str] | None = None) -> str:
    lines = [
        "Schlage für das Dokument vor:",
        "- title: kurzer, sprechender deutscher Titel (max. 60 Zeichen) nach dem Muster „Was – Wofür/Zeitraum“, "
        "z.B. „Stromrechnung Oktober 2026“ oder „Kfz-Versicherung Beitragsanpassung 2027“. Ohne Absendernamen.",
        f"- tags: bis zu {MAX_TAGS} passende Tags aus der Liste. Lieber wenige treffende als viele.",
        f"- new_tags: nur wenn wirklich kein vorhandener Tag passt, bis zu {MAX_NEW_TAGS} neue Tags, sonst leer.",
        "- correspondent / document_type: aus der Liste, leer wenn nichts passt.",
        "",
        "Vorhandene Tags: " + ", ".join(vocab["tags"]),
        "Vorhandene Korrespondenten: " + ", ".join(vocab["correspondents"]),
        "Vorhandene Dokumenttypen: " + ", ".join(vocab["document_types"]),
    ]
    if hist["count"] >= 3:
        lines.append(
            f"Andere Dokumente dieses Absenders ({hist['count']}) haben meist die Tags: {', '.join(hist['tags']) or 'keine'}"
            + (f" und den Typ {hist['document_type']}" if hist["document_type"] else "")
        )
    if examples:
        lines.append("So benennt der Nutzer Dokumente (Stil übernehmen): " + "; ".join(f"„{e}“" for e in examples))
    if rejected:
        lines.append("Diese Tags hat der Nutzer für diesen Absender abgelehnt, nicht vorschlagen: " + ", ".join(sorted(rejected)))
    lines += [
        "",
        f"Bisheriger Titel: {doc.title or '–'}",
        f"Korrespondent in Paperless: {doc.correspondent or '–'}",
        f"Dokumenttyp in Paperless: {doc.document_type or '–'}",
        f"Tags in Paperless: {', '.join(doc.tags or []) or '–'}",
        "",
        f'Text:\n"""\n{normalize(text)[:max_input]}\n"""',
    ]
    return "\n".join(lines)


def _pick(value, allowed: list[str]) -> str | None:
    """Wert auf einen vorhandenen Namen abbilden (Groß-/Kleinschreibung egal), sonst None."""
    lookup = {a.lower(): a for a in allowed}
    return lookup.get(str(value or "").strip().lower())


def ask_llm(client: OllamaClient, doc: Document, text: str, vocab: dict, hist: dict, rejected: set[str], max_input: int,
            examples: list[str] | None = None) -> dict:
    if not text.strip():
        raise OllamaError("Dokument hat keinen Text (OCR fehlt?)")
    prompt = build_prompt(doc, text, vocab, hist, rejected, max_input, examples)
    # Passt alles ins Kontextfenster? (grob 3 Zeichen je Token, Platz für die Antwort lassen)
    budget = (client.num_ctx - 600) * 3
    if len(prompt) > budget:
        prompt = build_prompt(doc, text, vocab, hist, rejected, max(800, max_input - (len(prompt) - budget)), examples)
    data = client.extract_json(SYSTEM, prompt, schema(vocab))
    title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip().strip('"„“')[:80]
    tags = [t for t in (_pick(v, vocab["tags"]) for v in data.get("tags") or []) if t]
    known = {t.lower() for t in vocab["tags"]}
    new_tags = []
    for raw in data.get("new_tags") or []:
        name = re.sub(r"\s+", " ", str(raw)).strip()[:40]
        if name and name.lower() not in known and name.lower() not in {n.lower() for n in new_tags}:
            new_tags.append(name)
    return {
        "title": title or None,
        "tags": list(dict.fromkeys(tags))[:MAX_TAGS],
        "new_tags": new_tags[:MAX_NEW_TAGS],
        "correspondent": _pick(data.get("correspondent"), vocab["correspondents"]),
        "document_type": _pick(data.get("document_type"), vocab["document_types"]),
    }


# --------------------------------------------------------------------------- Vorschlag erzeugen


def suggest(
    session: Session, doc: Document, paperless: PaperlessClient, ollama: OllamaClient | None, settings: Settings,
    names: dict | None = None,
) -> dict:
    """Vorschlag erzeugen und am Dokument speichern. Gibt Infos für das Aktivitätsprotokoll zurück."""
    names = names or {e: paperless.names(e) for e in ("tags", "correspondents", "document_types")}
    archive = _archive(session)
    vocab = vocabulary(session, names, archive)
    hist = history(session, doc, archive)
    _, corr, _ = effective(doc.tags, doc.correspondent, doc.document_type, doc.meta_accepted)
    rejected = rejected_tags(session, corr)
    in_paperless = {n.lower() for n in names.get("tags", {}).values()}
    suggestion: dict = {"tags": [], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    info = {"ai": False, "ai_seconds": None}

    def add_tag(name: str, source: str) -> None:
        if name.lower() in rejected or name.lower() in {t["name"].lower() for t in suggestion["tags"]}:
            return
        entry = {"name": name, "source": source}
        if name.lower() not in in_paperless:
            entry["new"] = True
        suggestion["tags"].append(entry)

    for tag in hist["tags"]:
        add_tag(tag, "history")
    if hist["document_type"]:
        suggestion["document_type"] = {"value": hist["document_type"], "source": "history"}

    if ollama is not None:
        import time

        started = time.monotonic()
        text = paperless.get_document(doc.id).get("content") or ""
        result = ask_llm(ollama, doc, text, vocab, hist, rejected, settings.meta_max_input_chars,
                         title_examples(session, corr))
        info.update(ai=True, ai_seconds=time.monotonic() - started)
        suggestion["model"] = ollama.model
        for tag in [*result["tags"], *result["new_tags"]]:
            add_tag(tag, "llm")
        if result["title"]:
            suggestion["title"] = {"value": result["title"], "source": "llm"}
        for key in ("correspondent", "document_type"):
            if result[key]:
                suggestion[key] = {"value": result[key], "source": "llm"}

    doc.meta_suggestion = suggestion
    doc.meta_state = "done"
    refresh_pending(doc)
    open_items = [i for i in pending(doc) if i["state"] == "open"]
    info["items"] = list(dict.fromkeys(p["label"] for p in open_items))
    info["pending"] = len(open_items)
    return info


# --------------------------------------------------------------------------- Entscheiden und übertragen


def _current(doc: Document, key: str) -> str | None:
    return None if key.startswith("tag:") else getattr(doc, key)


def _in_paperless(doc: Document, key: str, value: str) -> bool:
    if key.startswith("tag:"):
        return value.lower() in {t.lower() for t in doc.tags or []}
    return (_current(doc, key) or "").strip().lower() == value.strip().lower()


def pending(doc: Document, everything: bool = False) -> list[dict]:
    """Vorgemerkte (state=accepted) und offene (state=open) Unterschiede zu Paperless.

    Offene Vorschläge zeigt „Für Paperless“ (everything=False) nur mit echtem Nutzen: fehlende Tags,
    ein Titel nur, wenn der jetzige nichtssagend ist, Korrespondent/Dokumenttyp nur, wenn in Paperless leer.
    Die Dokumentseite (everything=True) zeigt auch abweichende Titel/Korrespondenten/Typen.
    Vorgemerktes erscheint immer, bis es übertragen ist oder Paperless denselben Wert hat.
    """
    sugg = doc.meta_suggestion or {}
    dismissed = doc.meta_dismissed or {}
    accepted = doc.meta_accepted or {}
    items = []
    for key, entry in accepted.items():
        if _in_paperless(doc, key, entry["value"]):
            continue
        items.append({"key": key, "label": "Tag" if key.startswith("tag:") else ITEMS[key], "current": _current(doc, key),
                      "new": entry["value"], "source": entry.get("source", "llm"), "new_tag": bool(entry.get("new_tag")),
                      "state": "accepted", "useful": True, "at": entry.get("at"), "suggested_at": sugg.get("at")})
    for tag in sugg.get("tags", []):
        key = f"tag:{tag['name']}"
        if key in accepted or key in dismissed or _in_paperless(doc, key, tag["name"]):
            continue
        items.append({"key": key, "label": "Tag", "current": None, "new": tag["name"], "source": tag["source"],
                      "new_tag": bool(tag.get("new")), "state": "open", "useful": True, "at": sugg.get("at")})
    for key in ("title", "correspondent", "document_type"):
        entry = sugg.get(key)
        if not entry or not entry.get("value") or key in accepted:
            continue
        value, current = entry["value"], _current(doc, key)
        if _in_paperless(doc, key, value) or dismissed.get(key) == value:
            continue
        useful = is_generic_title(current, doc.original_file_name) if key == "title" else not current
        if useful or everything:
            items.append({"key": key, "label": ITEMS[key], "current": current, "new": value, "source": entry["source"],
                          "new_tag": False, "state": "open", "useful": useful, "at": sugg.get("at")})
    return items


def refresh_pending(doc: Document) -> bool:
    doc.meta_pending = bool(pending(doc))
    return doc.meta_pending


def _record(session: Session, doc: Document, item: dict, decision: str, value: str | None) -> None:
    row = session.scalar(select(MetaDecision).where(MetaDecision.document_id == doc.id, MetaDecision.key == item["key"]))
    if row is None:
        row = MetaDecision(document_id=doc.id, key=item["key"], item="tag" if item["key"].startswith("tag:") else item["key"])
        session.add(row)
    _, corr, _ = effective(doc.tags, doc.correspondent, doc.document_type, doc.meta_accepted)
    suggested = ((doc.meta_suggestion or {}).get(item["key"]) or {}).get("value") if not item["key"].startswith("tag:") else None
    row.correspondent = corr
    row.suggested = suggested or (row.suggested if row.suggested else item["new"])
    row.value, row.source, row.decision = value, item["source"], decision
    row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.transferred_at = None


def accept(session: Session, doc: Document, keys: list[str], title: str | None = None) -> int:
    """Vorschläge vormerken – nur im Companion, die App lernt sofort daraus."""
    accepted = dict(doc.meta_accepted or {})
    count = 0
    for item in pending(doc, everything=True):
        if item["key"] not in keys:
            continue
        value = title.strip() if item["key"] == "title" and title and title.strip() else item["new"]
        if item["state"] == "accepted" and value == item["new"]:
            continue
        accepted[item["key"]] = {"value": value, "source": item["source"], "new_tag": item["new_tag"],
                                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        suggested = ((doc.meta_suggestion or {}).get(item["key"]) or {}).get("value", item["new"])
        _record(session, doc, item, "edited" if item["key"] == "title" and value != suggested else "accepted", value)
        count += 1
    doc.meta_accepted = dict(accepted) or None  # neues Objekt, damit SQLAlchemy die JSON-Änderung erkennt
    refresh_pending(doc)
    return count


def dismiss(session: Session, doc: Document, keys: list[str]) -> int:
    """Verwerfen (auch Vorgemerktes). Verworfene Tags schlägt die App für den Absender nicht mehr vor."""
    dismissed = dict(doc.meta_dismissed or {})
    accepted = dict(doc.meta_accepted or {})
    count = 0
    for item in pending(doc, everything=True):
        if item["key"] in keys:
            _record(session, doc, item, "dismissed", item["new"])
            accepted.pop(item["key"], None)
            dismissed[item["key"]] = item["new"]
            count += 1
    doc.meta_dismissed = dismissed or None
    doc.meta_accepted = accepted or None
    refresh_pending(doc)
    return count


def withdraw(session: Session, doc: Document, keys: list[str]) -> int:
    """Vormerkung zurücknehmen – der Vorschlag ist wieder offen."""
    accepted = dict(doc.meta_accepted or {})
    count = 0
    for key in keys:
        if accepted.pop(key, None) is not None:
            row = session.scalar(select(MetaDecision).where(MetaDecision.document_id == doc.id, MetaDecision.key == key))
            if row is not None and row.transferred_at is None:
                session.delete(row)
            count += 1
    doc.meta_accepted = accepted or None
    refresh_pending(doc)
    return count


def transfer(session: Session, paperless: PaperlessClient, doc: Document, keys: list[str] | None = None) -> list[str]:
    """Vorgemerkte Werte nach Paperless schreiben (alle oder die ausgewählten). Gibt die übertragenen Schlüssel zurück.

    Tags werden nur ergänzt (vorhandene bleiben), neue Tags beim Übertragen angelegt.
    """
    items = {i["key"]: i for i in pending(doc) if i["state"] == "accepted" and (keys is None or i["key"] in keys)}
    if not items:
        return []
    payload: dict = {}
    if "title" in items:
        payload["title"] = items["title"]["new"]
    for key, endpoint in (("correspondent", "correspondents"), ("document_type", "document_types")):
        if key in items:
            ids = {name.lower(): i for i, name in paperless.names(endpoint).items()}
            if items[key]["new"].lower() in ids:
                payload[key] = ids[items[key]["new"].lower()]
            else:
                items.pop(key)  # inzwischen in Paperless gelöscht
    tag_items = [i for k, i in items.items() if k.startswith("tag:")]
    if tag_items:
        current = paperless.get_document(doc.id).get("tags") or []
        tag_ids = {name.lower(): i for i, name in paperless.names("tags").items()}
        wanted = list(current)
        for item in tag_items:
            tag_id = tag_ids.get(item["new"].lower())
            if tag_id is None:
                tag_id = paperless.ensure_tag(item["new"])
                tag_ids[item["new"].lower()] = tag_id
            if tag_id not in wanted:
                wanted.append(tag_id)
        payload["tags"] = wanted
    if not payload:
        return []
    paperless.update_document(doc.id, payload)
    # Lokalen Spiegel sofort nachziehen (der nächste Abgleich bestätigt es)
    if "title" in payload:
        doc.title = payload["title"]
    if "correspondent" in payload:
        doc.correspondent = items["correspondent"]["new"]
    if "document_type" in payload:
        doc.document_type = items["document_type"]["new"]
    if tag_items:
        have = {t.lower() for t in doc.tags or []}
        doc.tags = [*(doc.tags or []), *[i["new"] for i in tag_items if i["new"].lower() not in have]]
    accepted = {k: v for k, v in (doc.meta_accepted or {}).items() if k not in items}
    doc.meta_accepted = accepted or None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in session.scalars(select(MetaDecision).where(MetaDecision.document_id == doc.id, MetaDecision.key.in_(list(items)))):
        row.transferred_at = now
    refresh_pending(doc)
    return list(items)


def quality(session: Session) -> dict:
    """Trefferquote deiner Entscheidungen je Quelle und Art – so siehst du, wie gut die Vorschläge sind."""
    table: dict = {}
    for item, source, decision in session.execute(select(MetaDecision.item, MetaDecision.source, MetaDecision.decision)):
        row = table.setdefault((source, item), {"accepted": 0, "edited": 0, "dismissed": 0})
        row[decision] = row.get(decision, 0) + 1
    rows = []
    for (source, item), counts in sorted(table.items()):
        total = sum(counts.values())
        rows.append({"source": source, "item": item, **counts, "total": total,
                     "rate": round(100 * (counts["accepted"] + counts["edited"]) / total) if total else 0})
    waiting = session.scalar(select(func.count()).select_from(MetaDecision).where(
        MetaDecision.decision.in_(("accepted", "edited")), MetaDecision.transferred_at.is_(None)))
    transferred = session.scalar(select(func.count()).select_from(MetaDecision).where(MetaDecision.transferred_at.is_not(None)))
    return {"rows": rows, "waiting": waiting, "transferred": transferred}


def archive_hints(session: Session) -> dict:
    """Wo lohnt sich ein Vorschlag? Zahlen für die Seite „Für Paperless“."""
    untagged = generic = no_corr = 0
    for tags, title, file_name, corr, accepted in session.execute(
        select(Document.tags, Document.title, Document.original_file_name, Document.correspondent, Document.meta_accepted)
    ):
        tags, corr, _ = effective(tags, corr, None, accepted)
        title = (accepted or {}).get("title", {}).get("value", title)
        untagged += not tags
        generic += is_generic_title(title, file_name)
        no_corr += not corr
    return {"untagged": untagged, "generic_title": generic, "no_correspondent": no_corr}


def matches_hint(doc: Document, hint: str) -> bool:
    tags, corr, _ = effective(doc.tags, doc.correspondent, None, doc.meta_accepted)
    if hint == "untagged":
        return not tags
    if hint == "generic_title":
        return is_generic_title((doc.meta_accepted or {}).get("title", {}).get("value", doc.title), doc.original_file_name)
    if hint == "no_correspondent":
        return not corr
    if hint == "meta_pending":
        return bool(doc.meta_pending)
    return True


HINTS = {
    "untagged": "ohne Tags",
    "generic_title": "mit nichtssagendem Titel",
    "no_correspondent": "ohne Korrespondent",
    "meta_pending": "mit offenem Metadaten-Vorschlag",
}
