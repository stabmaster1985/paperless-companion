"""Serien wiederkehrender Dokumente (Lohnabrechnungen, Kontoauszüge, Abschläge …).

Eine Serie sind Dokumente desselben Absenders mit gleichem Betreff (Titel ohne Monat, Jahr und Zahlen). Aus den
schon abgelegten Mitgliedern – Werte in Paperless und alles, was du vorgemerkt hast – entsteht ein Profil:
übliche Tags, Dokumenttyp, Korrespondent und das Titelmuster (z.B. „Lohnabrechnung {Monat} {JJJJ}“).
Neue Mitglieder bekommen daraus Vorschläge mit der Quelle „Serie“ – ohne AI, einheitlich und auf Deutsch.

Nichts geht verloren: Vorgemerktes und Verworfenes bleiben unberührt und zählen im Profil wie Paperless-Werte.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metadata
from app.models import Document, KeyValue

MIN_SIZE = 3  # ab so vielen Dokumenten ist es eine Serie
MIN_REFS = 2  # so viele abgelegte Mitglieder braucht ein Profilwert
SHARE = 0.6  # Anteil der abgelegten Mitglieder, die einen Wert tragen müssen
AUTO_REFS = 5  # automatisch vormerken erst ab so vielen einheitlichen Vorbildern
KV_KEY = "series"

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
          "Dezember"]
_MONTH_FULL = {m.lower(): i + 1 for i, m in enumerate(MONTHS)} | {"maerz": 3}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
               "okt": 10, "nov": 11, "dez": 12}
_ABBR = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
_YEAR = r"(?:19|20)\d{2}"
# Reihenfolge wichtig: zuerst die spezifischen Schreibweisen
_PATTERNS = [
    (re.compile(rf"\b(\d{{1,2}})\.(\d{{1,2}})\.({_YEAR})\b"), "{TT}.{MM}.{JJJJ}"),
    (re.compile(rf"\b({'|'.join(_MONTH_FULL)})\s+({_YEAR})\b", re.IGNORECASE), "{Monat} {JJJJ}"),
    (re.compile(rf"\b({'|'.join(_MONTH_ABBR)})\.?\s+({_YEAR})\b", re.IGNORECASE), "{Mon} {JJJJ}"),
    (re.compile(rf"\b({_YEAR})-(0[1-9]|1[0-2])\b"), "{JJJJ}-{MM}"),
    (re.compile(rf"\b(0?[1-9]|1[0-2])([/.\-])({_YEAR})\b"), None),  # Trenner bleibt erhalten
    (re.compile(rf"\bQ([1-4])\s*[/ ]\s*({_YEAR})\b", re.IGNORECASE), "Q{Q} {JJJJ}"),
    (re.compile(rf"\b({_YEAR})\b"), "{JJJJ}"),
]


def title_pattern(title: str | None) -> str | None:
    """Titel -> Muster mit Platzhaltern: „Lohnabrechnung Mai 2026“ -> „Lohnabrechnung {Monat} {JJJJ}“."""
    text = " ".join((title or "").split())
    if not text or not metadata.title_words(text):
        return None
    for regex, placeholder in _PATTERNS:
        if placeholder is None:
            text = regex.sub(lambda m: "{MM}" + m.group(2) + "{JJJJ}", text)
        else:
            text = regex.sub(placeholder, text)
    return text


def fill_pattern(pattern: str, day: date | None, month_offset: int = 0) -> str | None:
    """Muster mit dem Dokumentdatum füllen; month_offset -1: Abrechnung für den Vormonat."""
    if "{" not in pattern:
        return pattern
    if day is None:
        return None
    index = day.year * 12 + day.month - 1 + month_offset
    year, month = divmod(index, 12)
    month += 1
    return (pattern.replace("{TT}", f"{day.day:02d}").replace("{Monat}", MONTHS[month - 1]).replace("{Mon}", _ABBR[month - 1])
            .replace("{MM}", f"{month:02d}").replace("{Q}", str((month - 1) // 3 + 1)).replace("{JJJJ}", str(year)))


@dataclass
class Series:
    id: str
    correspondent: str | None
    subject: tuple[str, ...]
    members: list[Document] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    document_type: str | None = None
    pattern: str | None = None
    month_offset: int = 0
    refs: int = 0  # abgelegte Mitglieder (Vorbilder)
    example: str = ""

    @property
    def name(self) -> str:
        base = self.pattern or self.example or " ".join(self.subject).title()
        name = re.sub(r"\s*\{[^}]+\}", "", base)  # Platzhalter weg …
        name = re.sub(r"(?<![\w-])\d+(?![\w-])", "", name)  # … und laufende Nummern („Rechnung Online-Shop 2“)
        return " ".join(name.split()).strip(" -–/.") or base

    @property
    def uniform(self) -> bool:
        """Einheitlich genug zum automatischen Vormerken?"""
        return self.refs >= AUTO_REFS

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "correspondent": self.correspondent, "size": len(self.members),
                "refs": self.refs, "tags": self.tags, "document_type": self.document_type, "pattern": self.pattern,
                "month_offset": self.month_offset}


def _values(doc: Document) -> tuple[list[str], str | None, str | None, str | None]:
    """Werte inklusive Vorgemerktem: Tags (ohne Jahres-Tags und unaufgelöste), Korrespondent, Typ, Titel."""
    tags, corr, doctype = metadata.effective(doc.tags, doc.correspondent, doc.document_type, doc.meta_accepted)
    tags = [t for t in tags if not metadata.is_year_tag(t) and not metadata.is_unresolved_tag(t)]
    return tags, corr, doctype, metadata._effective_title(doc)


def _subject(doc: Document) -> frozenset[str]:
    return frozenset(metadata._subject_words(doc) - metadata.SCANNER_WORDS)


def _series_id(corr: str | None, subject) -> str:
    return hashlib.sha1(f"{(corr or '').lower()}|{' '.join(sorted(subject))}".encode()).hexdigest()[:10]


def build(session: Session) -> list[Series]:
    """Alle Serien mit Profil, größte zuerst."""
    groups: dict[tuple[str | None, frozenset], list[Document]] = {}
    loose: list[tuple[Document, frozenset]] = []  # ohne Korrespondent
    for doc in session.scalars(select(Document)):
        subject = _subject(doc)
        if not subject:
            continue
        _, corr, _, _ = _values(doc)
        if corr:
            groups.setdefault((corr, subject), []).append(doc)
        else:
            loose.append((doc, subject))
    # Dokumente ohne Korrespondent: zur einzigen Serie mit gleichem Betreff, sonst eigene Gruppe
    by_subject: dict[frozenset, list[tuple]] = {}
    for key in groups:
        by_subject.setdefault(key[1], []).append(key)
    for doc, subject in loose:
        keys = by_subject.get(subject, [])
        key = keys[0] if len(keys) == 1 else (None, subject)
        groups.setdefault(key, []).append(doc)

    result = []
    for (corr, subject), docs in groups.items():
        if len(docs) < MIN_SIZE:
            continue
        s = Series(_series_id(corr, subject), corr, tuple(sorted(subject)), sorted(docs, key=lambda d: (d.created or date.min, d.id)))
        _profile(s)
        result.append(s)
    result.sort(key=lambda s: (-len(s.members), s.name.lower()))
    return result


def _profile(s: Series) -> None:
    values = [(_values(d), d) for d in s.members]
    with_tags = [v for v, _ in values if v[0]]
    if len(with_tags) >= MIN_REFS:
        counts = Counter(t for v in with_tags for t in set(v[0]))
        s.tags = sorted(t for t, n in counts.items() if n / len(with_tags) >= SHARE)
    types = [v[2] for v, _ in values if v[2]]
    if len(types) >= MIN_REFS:
        name, n = Counter(types).most_common(1)[0]
        s.document_type = name if n / len(types) >= SHARE else None
    # Titelmuster aus sprechenden Titeln; prüfen, ob es sich aus dem Dokumentdatum reproduzieren lässt
    titled = [(title_pattern(v[3]), v[3], d) for v, d in values
              if v[3] and not metadata.GENERIC_TITLE.fullmatch(v[3].strip()) and metadata.title_words(v[3])]
    titled = [t for t in titled if t[0]]
    s.refs = max(len(with_tags), len(types))  # abgelegt = trägt schon Tags bzw. Typ (oder einen Titel nach Muster)
    if titled:
        s.example = titled[-1][1]
    if len(titled) >= MIN_REFS:
        pattern, n = Counter(p for p, _, _ in titled).most_common(1)[0]
        if n >= MIN_REFS and n / len(titled) >= 0.5:
            same = [(title, d) for p, title, d in titled if p == pattern]
            s.refs, s.example = max(s.refs, n), same[-1][0]
            best, best_hits = 0, -1
            for offset in (0, -1, 1):
                hits = sum(fill_pattern(pattern, d.created, offset) == " ".join(title.split()) for title, d in same)
                if hits > best_hits:
                    best, best_hits = offset, hits
            # nur verwenden, wenn das Datum der Vorbilder zu ihren Titeln passt (sonst würde der Monat falsch)
            if "{" not in pattern or best_hits / len(same) >= SHARE:
                s.pattern, s.month_offset = pattern, best


def wanted(s: Series, doc: Document) -> dict:
    """Was dem Dokument laut Serienprofil fehlt: {"title": …, "tags": [...], "document_type": …, "correspondent": …}."""
    tags, corr, doctype, title = _values(doc)
    result: dict = {}
    if s.pattern:
        want = fill_pattern(s.pattern, doc.created, s.month_offset)
        if want and want != " ".join((title or "").split()):
            result["title"] = want
    have = {t.lower() for t in tags}
    missing = [t for t in s.tags if t.lower() not in have]
    if missing:
        result["tags"] = missing
    if s.document_type and not doctype:
        result["document_type"] = s.document_type
    if s.correspondent and not corr:
        result["correspondent"] = s.correspondent
    return result


def overlay(doc: Document, s: Series | None, year_mode: str = "date", known_tags: set[str] | None = None) -> bool:
    """Serienvorschläge in den gespeicherten Vorschlag einarbeiten und ihn aufräumen. True, wenn sich etwas änderte.

    Serienwerte haben Vorrang vor AI und Verlauf; AI-Tags außerhalb des Profils bleiben höchstens einer.
    """
    before = doc.meta_suggestion
    if before is None and s is None:
        return False
    sugg = dict(before or {"tags": [], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    # zuerst aufräumen (Jahres-Tags, Englisch), dann die Serienwerte einarbeiten
    sugg = metadata.clean_suggestion(sugg, doc, year_mode, known_tags)
    tags = [t for t in sugg.get("tags") or [] if t.get("source") != "series"]
    for key in ("title", "document_type", "correspondent"):
        if (sugg.get(key) or {}).get("source") == "series":
            sugg.pop(key)
    if s is not None:
        want = wanted(s, doc)
        if s.tags:
            profile = {t.lower() for t in s.tags}
            extra = [t for t in tags if t.get("source") == "llm" and t["name"].lower() not in profile and not t.get("new")]
            keep = {id(t) for t in extra[:1]}
            tags = [t for t in tags if t.get("source") != "llm" or t["name"].lower() in profile or id(t) in keep]
        known = {t["name"].lower() for t in tags}
        tags = [{"name": t, "source": "series"} for t in want.get("tags", []) if t.lower() not in known] + tags
        if "title" in want:
            sugg["title"] = {"value": want["title"], "source": "series"}
        elif s.pattern:
            sugg.pop("title", None)  # Titel entspricht schon dem Muster der Serie
        for key in ("document_type", "correspondent"):
            if key in want:
                sugg[key] = {"value": want[key], "source": "series"}
    sugg["tags"] = tags
    if before is not None and _comparable(sugg) == _comparable(before):
        return False
    if before is None and not sugg["tags"] and not any(k in sugg for k in ("title", "document_type", "correspondent")):
        return False
    doc.meta_suggestion = sugg
    if doc.meta_state is None:
        doc.meta_state = "done"
    metadata.refresh_pending(doc)
    return True


def _comparable(sugg: dict) -> str:
    return json.dumps({k: v for k, v in sugg.items() if k != "at"}, sort_keys=True, ensure_ascii=False)


def refresh(session: Session, year_mode: str = "date", auto_accept: bool = False) -> dict:
    """Serien neu berechnen, Zugehörigkeit speichern, Vorschläge einarbeiten. Gibt Zähler zurück."""
    all_series = build(session)
    by_doc = {d.id: s for s in all_series for d in s.members}
    known_tags = {t for _, tags, _, _ in metadata._archive(session) for t in tags}
    changed = accepted = 0
    for doc in session.scalars(select(Document)):
        s = by_doc.get(doc.id)
        key = s.id if s else None
        if doc.series_key != key:
            doc.series_key = key
        if overlay(doc, s, year_mode, known_tags):
            changed += 1
        if auto_accept and s is not None and s.uniform and not doc.meta_dismissed:
            keys = [i["key"] for i in metadata.pending(doc, everything=True)
                    if i["state"] == "open" and i["source"] == "series"]
            if keys:
                accepted += metadata.accept(session, doc, keys)
    value = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "series": [s.as_dict() for s in all_series]}, ensure_ascii=False)
    item = session.get(KeyValue, KV_KEY)
    if item:
        item.value = value
    else:
        session.add(KeyValue(key=KV_KEY, value=value))
    return {"series": len(all_series), "changed": changed, "accepted": accepted}


def stored(session: Session) -> dict:
    item = session.get(KeyValue, KV_KEY)
    return json.loads(item.value) if item else {"at": None, "series": []}


def open_counts(session: Session) -> dict[str, dict]:
    """Je Serie: Dokumente und offene Serienvorschläge (für die Karten auf „Für Paperless“)."""
    counts: dict[str, dict] = {}
    for doc in session.scalars(select(Document).where(Document.series_key.is_not(None), Document.meta_pending.is_(True))):
        items = [i for i in metadata.pending(doc) if i["state"] == "open" and i["source"] == "series"]
        if items:
            c = counts.setdefault(doc.series_key, {"docs": 0, "items": 0})
            c["docs"] += 1
            c["items"] += len(items)
    return counts


def workflow(s: dict, review_tag: str = "") -> dict:
    """Vorlage für einen Paperless-Workflow, der neue Dokumente der Serie schon beim Einlesen einordnet.

    Erkannt wird am Betreff im Text bzw. Dateinamen; gesetzt werden Korrespondent, Typ, Tags und der Titel nach Muster.
    """
    words = [w for w in s["name"].split() if len(w) > 2] or s["name"].split()
    title = None
    if s.get("pattern"):
        title = (s["pattern"].replace("{Monat}", "{created_month_name}").replace("{Mon}", "{created_month_name_short}")
                 .replace("{MM}", "{created_month}").replace("{TT}", "{created_day}").replace("{JJJJ}", "{created_year}")
                 .replace("{Q}", "{created_month}"))
    tags = [*s.get("tags", []), *([review_tag] if review_tag else [])]
    return {
        "name": f"Serie: {s['name']}" + (f" ({s['correspondent']})" if s.get("correspondent") else ""),
        "trigger": {"type": "Dokument hinzugefügt", "matching_algorithm": "alle Wörter", "match": " ".join(words),
                    "filter_has_correspondent": s.get("correspondent")},
        "action": {"type": "Zuweisung", "assign_title": title, "assign_correspondent": s.get("correspondent"),
                   "assign_document_type": s.get("document_type"), "assign_tags": tags},
        "note": ("Der Titel bezieht sich auf das Dokumentdatum."
                 + (" Achtung: Bei dieser Serie gilt der Titel für den Vormonat – das kann ein Workflow nicht abbilden, "
                    "den Titel dann weiter über den Companion vormerken." if s.get("month_offset") else "")),
    }
