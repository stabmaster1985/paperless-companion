"""Schlanker Client für die Paperless-NGX-REST-API."""

import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import unquote

import httpx

# Custom Fields, die beim Rückschreiben gepflegt werden: Name -> Paperless-Datentyp
WRITEBACK_FIELDS = {
    "Betrag": "monetary",
    "Fällig am": "date",
    "Kündigung bis": "date",
    "Vertragsende": "date",
    "Garantie bis": "date",
}


class PaperlessError(RuntimeError):
    pass


class PaperlessClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Token {token}", "Accept": "application/json; version=5"},
            timeout=timeout,
            transport=transport,
        )
        self._custom_field_ids: dict[str, int] = {}

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params) -> dict:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"GET {path}: {exc}") from exc
        return response.json()

    def _paginate(self, path: str, **params) -> Iterator[dict]:
        page = 1
        while True:
            data = self._get(path, page=page, **params)
            yield from data.get("results", [])
            if not data.get("next"):
                return
            page += 1

    def ping(self) -> bool:
        self._get("/api/documents/", page_size=1)
        return True

    def names(self, endpoint: str) -> dict[int, str]:
        return {item["id"]: item["name"] for item in self._paginate(f"/api/{endpoint}/", page_size=500)}

    def documents_modified_after(self, since_iso: str | None) -> Iterator[dict]:
        params = {"page_size": 50, "ordering": "modified"}
        if since_iso:
            params["modified__gt"] = since_iso
        yield from self._paginate("/api/documents/", **params)

    def get_document(self, doc_id: int) -> dict:
        return self._get(f"/api/documents/{doc_id}/")

    def all_documents_metadata(self) -> tuple[int, Iterator[dict]]:
        """Alle Dokumente ohne Inhalt (für den Archiv-Index). Liefert (Anzahl, Iterator)."""
        params = {
            "page_size": 100,
            "ordering": "id",
            "truncate_content": "true",
            "fields": "id,title,correspondent,document_type,created,created_date,modified,tags,"
                      "page_count,original_file_name",
        }
        count = self._get("/api/documents/", page_size=1).get("count", 0)
        return count, self._paginate("/api/documents/", **params)

    def download(self, doc_id: int, original: bool = False) -> tuple[bytes, str]:
        """Lädt die (archivierte) Datei. Gibt (Inhalt, Dateiname) zurück."""
        try:
            response = self._client.get(
                f"/api/documents/{doc_id}/download/", params={"original": "true"} if original else None, timeout=120
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Download {doc_id}: {exc}") from exc
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*=utf-8''([^;]+)", disposition, re.IGNORECASE) or re.search(
            r'filename="?([^";]+)"?', disposition
        )
        name = unquote(match.group(1)) if match else f"{doc_id}.pdf"
        return response.content, name

    def ensure_tag(self, name: str) -> int:
        for item in self._paginate("/api/tags/", page_size=500, name__iexact=name):
            if item["name"].lower() == name.lower():
                return item["id"]
        try:
            response = self._client.post("/api/tags/", json={"name": name, "matching_algorithm": 0})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Tag {name!r} anlegen: {exc}") from exc
        return response.json()["id"]

    def add_tag(self, doc_ids: list[int], tag_name: str) -> None:
        tag_id = self.ensure_tag(tag_name)
        payload = {"documents": doc_ids, "method": "add_tag", "parameters": {"tag": tag_id}}
        try:
            self._client.post("/api/documents/bulk_edit/", json=payload).raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Tag setzen: {exc}") from exc

    def delete_document(self, doc_id: int) -> None:
        """Löscht ein Dokument (landet in Paperless im Papierkorb)."""
        try:
            self._client.delete(f"/api/documents/{doc_id}/").raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Löschen {doc_id}: {exc}") from exc

    # ------------------------------------------------------------------ Custom Fields

    def ensure_custom_fields(self) -> dict[str, int]:
        if self._custom_field_ids:
            return self._custom_field_ids
        existing = {item["name"]: item["id"] for item in self._paginate("/api/custom_fields/", page_size=500)}
        for name, data_type in WRITEBACK_FIELDS.items():
            if name not in existing:
                try:
                    response = self._client.post("/api/custom_fields/", json={"name": name, "data_type": data_type})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise PaperlessError(f"Custom Field {name!r} anlegen: {exc}") from exc
                existing[name] = response.json()["id"]
        self._custom_field_ids = {name: existing[name] for name in WRITEBACK_FIELDS}
        return self._custom_field_ids

    def set_custom_fields(self, doc_id: int, values: dict[str, object]) -> None:
        """Setzt die angegebenen Felder, andere Custom Fields des Dokuments bleiben erhalten."""
        ids = self.ensure_custom_fields()
        doc = self.get_document(doc_id)
        managed = {ids[name] for name in values}
        merged = [cf for cf in doc.get("custom_fields", []) if cf["field"] not in managed]
        for name, value in values.items():
            if value is None:
                continue
            if isinstance(value, date):
                value = value.isoformat()
            elif WRITEBACK_FIELDS[name] == "monetary":
                value = f"EUR{float(value):.2f}"
            merged.append({"field": ids[name], "value": value})
        try:
            response = self._client.patch(f"/api/documents/{doc_id}/", json={"custom_fields": merged})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Dokument {doc_id} aktualisieren: {exc}") from exc
