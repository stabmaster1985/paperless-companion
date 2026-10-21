"""Vorschläge für Paperless-Metadaten: Titel, Tags, Korrespondent, Dokumenttyp.

Die Vorschläge werden nur im Companion gespeichert. Nach Paperless geschrieben wird erst,
wenn du sie einzeln übernimmst (siehe ``approve``).

Zwei Quellen:
- „Verlauf“: Was deine übrigen Dokumente desselben Korrespondenten in Paperless tragen
  (kostet keine Rechenzeit, lernt aus dem, was du in Paperless pflegst und übernimmst).
- „AI“: Das lokale Modell liest den Anfang des Dokuments und wählt aus deinen vorhandenen
  Tags, Korrespondenten und Dokumenttypen (per JSON-Schema auf diese Listen beschränkt).
Abgelehnte Vorschläge merkt sich die App und schlägt sie für den Korrespondenten nicht erneut vor.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.extraction.rules import normalize
from app.models import Document
from app.ollama import OllamaClient, OllamaError
from app.paperless import PaperlessClient

ITEMS = {"title": "Titel", "correspondent": "Korrespondent", "document_type": "Dokumenttyp", "tags": "Tags"}
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


def vocabulary(session: Session, names: dict) -> dict[str, list[str]]:
    """Vorhandene Tags/Korrespondenten/Dokumenttypen aus Paperless, die häufig benutzten zuerst."""
    usage: dict[str, Counter] = {"tags": Counter(), "correspondents": Counter(), "document_types": Counter()}
    for tags, corr, doctype in session.execute(select(Document.tags, Document.correspondent, Document.document_type)):
        usage["tags"].update(tags or [])
        if corr:
            usage["correspondents"][corr] += 1
        if doctype:
            usage["document_types"][doctype] += 1
    result = {}
    for key in usage:
        available = list(names.get(key, {}).values())
        available.sort(key=lambda n: (-usage[key][n], n.lower()))
        result[key] = available[:VOCAB_LIMIT] if key != "document_types" else available
    return result


def rejected_tags(session: Session, correspondent: str | None) -> set[str]:
    """Für diesen Korrespondenten bereits abgelehnte Tags (kleingeschrieben)."""
    if not correspondent:
        return set()
    rejected: set[str] = set()
    for dismissed in session.scalars(
        select(Document.meta_dismissed).where(Document.correspondent == correspondent, Document.meta_dismissed.is_not(None))
    ):
        rejected.update(k[4:].lower() for k in dismissed or {} if k.startswith("tag:"))
    return rejected


def history(session: Session, doc: Document) -> dict:
    """Was tragen die anderen Dokumente desselben Korrespondenten? (mind. 3 Dokumente)"""
    if not doc.correspondent:
        return {"count": 0, "tags": [], "document_type": None}
    rows = session.execute(
        select(Document.tags, Document.document_type).where(Document.correspondent == doc.correspondent, Document.id != doc.id)
    ).all()
    count = len(rows)
    tags = Counter(t for tag_list, _ in rows for t in set(tag_list or []))
    types = Counter(t for _, t in rows if t)
    common_tags = [t for t, n in tags.most_common() if count >= 3 and n / count >= 0.5]
    doctype = None
    if types and count >= 3:
        name, n = types.most_common(1)[0]
        doctype = name if n / count >= 0.6 else None
    return {"count": count, "tags": common_tags, "document_type": doctype}


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


def build_prompt(doc: Document, text: str, vocab: dict, hist: dict, rejected: set[str], max_input: int) -> str:
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


def ask_llm(client: OllamaClient, doc: Document, text: str, vocab: dict, hist: dict, rejected: set[str], max_input: int) -> dict:
    if not text.strip():
        raise OllamaError("Dokument hat keinen Text (OCR fehlt?)")
    prompt = build_prompt(doc, text, vocab, hist, rejected, max_input)
    # Passt alles ins Kontextfenster? (grob 3 Zeichen je Token, Platz für die Antwort lassen)
    budget = (client.num_ctx - 600) * 3
    if len(prompt) > budget:
        prompt = build_prompt(doc, text, vocab, hist, rejected, max(800, max_input - (len(prompt) - budget)))
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
    vocab = vocabulary(session, names)
    hist = history(session, doc)
    rejected = rejected_tags(session, doc.correspondent)
    suggestion: dict = {"tags": [], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    info = {"ai": False, "ai_seconds": None}

    for tag in hist["tags"]:
        if tag.lower() not in rejected:
            suggestion["tags"].append({"name": tag, "source": "history"})
    if hist["document_type"]:
        suggestion["document_type"] = {"value": hist["document_type"], "source": "history"}

    if ollama is not None:
        import time

        started = time.monotonic()
        text = paperless.get_document(doc.id).get("content") or ""
        result = ask_llm(ollama, doc, text, vocab, hist, rejected, settings.meta_max_input_chars)
        info.update(ai=True, ai_seconds=time.monotonic() - started)
        suggestion["model"] = ollama.model
        have = {t["name"].lower() for t in suggestion["tags"]}
        for tag in result["tags"]:
            if tag.lower() not in have and tag.lower() not in rejected:
                suggestion["tags"].append({"name": tag, "source": "llm"})
                have.add(tag.lower())
        for tag in result["new_tags"]:
            if tag.lower() not in have and tag.lower() not in rejected:
                suggestion["tags"].append({"name": tag, "source": "llm", "new": True})
                have.add(tag.lower())
        if result["title"]:
            suggestion["title"] = {"value": result["title"], "source": "llm"}
        for key in ("correspondent", "document_type"):
            if result[key]:
                suggestion[key] = {"value": result[key], "source": "llm"}

    doc.meta_suggestion = suggestion
    doc.meta_state = "done"
    refresh_pending(doc)
    open_items = pending(doc)
    info["items"] = list(dict.fromkeys(p["label"] for p in open_items))
    info["pending"] = len(open_items)
    return info


# --------------------------------------------------------------------------- Abgleich mit Paperless


def pending(doc: Document, everything: bool = False) -> list[dict]:
    """Offene Unterschiede Vorschlag -> Paperless.

    Für „Für Paperless“ (everything=False) nur, was echten Nutzen hat: fehlende Tags, ein Titel nur,
    wenn der jetzige nichtssagend ist, Korrespondent/Dokumenttyp nur, wenn in Paperless leer.
    Auf der Dokumentseite (everything=True) auch abweichende Titel/Korrespondenten/Typen.
    """
    sugg = doc.meta_suggestion or {}
    dismissed = doc.meta_dismissed or {}
    items = []
    current_tags = {t.lower() for t in doc.tags or []}
    for tag in sugg.get("tags", []):
        key = f"tag:{tag['name']}"
        if tag["name"].lower() in current_tags or key in dismissed:
            continue
        items.append({"key": key, "label": "Tag", "current": None, "new": tag["name"], "source": tag["source"],
                      "new_tag": bool(tag.get("new"))})
    for key, current in (("title", doc.title), ("correspondent", doc.correspondent), ("document_type", doc.document_type)):
        entry = sugg.get(key)
        if not entry or not entry.get("value"):
            continue
        value = entry["value"]
        if (current or "").strip().lower() == value.strip().lower() or dismissed.get(key) == value:
            continue
        useful = is_generic_title(current, doc.original_file_name) if key == "title" else not current
        if useful or everything:
            items.append({"key": key, "label": ITEMS[key], "current": current, "new": value, "source": entry["source"],
                          "useful": useful})
    return items


def refresh_pending(doc: Document) -> bool:
    doc.meta_pending = bool(pending(doc))
    return doc.meta_pending


def dismiss(doc: Document, keys: list[str]) -> int:
    """Vorschläge verwerfen; abgelehnte Tags werden für den Korrespondenten nicht mehr vorgeschlagen."""
    dismissed = dict(doc.meta_dismissed or {})
    count = 0
    for item in pending(doc, everything=True):
        if item["key"] in keys:
            dismissed[item["key"]] = item["new"]
            count += 1
    doc.meta_dismissed = dismissed or None
    refresh_pending(doc)
    return count


def approve(
    paperless: PaperlessClient, doc: Document, keys: list[str], title_override: str | None = None
) -> list[str]:
    """Ausgewählte Vorschläge in Paperless schreiben. Gibt die übernommenen Schlüssel zurück.

    Tags werden nur ergänzt (vorhandene bleiben), neue Tags nur angelegt, wenn ausgewählt.
    """
    items = {i["key"]: i for i in pending(doc, everything=True) if i["key"] in keys}
    if title_override is not None and title_override.strip() and "title" in keys:
        items.setdefault("title", {"key": "title", "new": title_override.strip()})["new"] = title_override.strip()
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
    added_tags: list[str] = []
    if tag_items:
        current = paperless.get_document(doc.id).get("tags") or []
        tag_ids = {name.lower(): i for i, name in paperless.names("tags").items()}
        wanted = list(current)
        for item in tag_items:
            tag_id = tag_ids.get(item["new"].lower())
            if tag_id is None:
                if not item.get("new_tag"):
                    items.pop(item["key"])
                    continue
                tag_id = paperless.ensure_tag(item["new"])
            if tag_id not in wanted:
                wanted.append(tag_id)
            added_tags.append(item["new"])
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
    if added_tags:
        doc.tags = [*(doc.tags or []), *[t for t in added_tags if t.lower() not in {x.lower() for x in doc.tags or []}]]
    refresh_pending(doc)
    return list(items)


def archive_hints(session: Session) -> dict:
    """Wo lohnt sich ein Vorschlag? Zahlen für die Seite „Für Paperless“."""
    untagged = generic = no_corr = 0
    for tags, title, file_name, corr in session.execute(
        select(Document.tags, Document.title, Document.original_file_name, Document.correspondent)
    ):
        untagged += not tags
        generic += is_generic_title(title, file_name)
        no_corr += not corr
    return {"untagged": untagged, "generic_title": generic, "no_correspondent": no_corr}


def matches_hint(doc: Document, hint: str) -> bool:
    if hint == "untagged":
        return not doc.tags
    if hint == "generic_title":
        return is_generic_title(doc.title, doc.original_file_name)
    if hint == "no_correspondent":
        return not doc.correspondent
    if hint == "meta_pending":
        return bool(doc.meta_pending)
    return True


HINTS = {
    "untagged": "ohne Tags",
    "generic_title": "mit nichtssagendem Titel",
    "no_correspondent": "ohne Korrespondent",
    "meta_pending": "mit offenem Metadaten-Vorschlag",
}
