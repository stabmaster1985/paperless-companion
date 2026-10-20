"""Kurze KI-Zusammenfassungen mit dem lokalen Modell.

Kleine Modelle liefern brauchbare Ergebnisse, wenn sie nur den Anfang des Dokuments,
eine feste Gliederung und eine harte Längengrenze bekommen.
"""

from app.extraction.rules import normalize
from app.fields import shorten
from app.ollama import OllamaClient, OllamaError

SYSTEM = (
    "Du fasst deutsche Dokumente (OCR-Text, kann Fehler enthalten) sehr knapp auf Deutsch zusammen. "
    "Nenne nur, was im Text steht. Antworte ausschließlich mit JSON nach dem Schema."
)
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
DEFAULT_LIMIT = 400


def build_prompt(title: str, correspondent: str | None, text: str, limit: int, max_input: int) -> str:
    body = normalize(text)[:max_input]
    return (
        f"Fasse das Dokument in höchstens {limit} Zeichen zusammen: wer schreibt, worum es geht, "
        "wichtige Beträge oder Fristen und ob etwas zu tun ist. Kein einleitendes „Das Dokument …“, "
        "keine Aufzählungszeichen.\n\n"
        f"Titel: {title}\nAbsender: {correspondent or 'unbekannt'}\n\nText:\n\"\"\"\n{body}\n\"\"\""
    )


def generate(client: OllamaClient, title: str, correspondent: str | None, text: str, limit: int, max_input: int) -> str:
    if not text.strip():
        raise OllamaError("Dokument hat keinen Text (OCR fehlt?)")
    data = client.extract_json(SYSTEM, build_prompt(title, correspondent, text, limit, max_input), SCHEMA)
    summary = str(data.get("summary") or "").strip().strip('"')
    if not summary:
        raise OllamaError("Leere Zusammenfassung vom Modell")
    return shorten(summary, limit)
