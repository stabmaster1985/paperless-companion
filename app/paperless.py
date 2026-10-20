"""Schlanker Client für die Paperless-NGX-REST-API."""

import re
from collections.abc import Iterator
from urllib.parse import unquote

import httpx

class PaperlessError(RuntimeError):
    pass


class PaperlessClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            # Bewusst ohne feste API-Version: Paperless nutzt dann seine aktuelle Standardversion.
            # Eine fest angefragte, nicht mehr unterstützte Version beantwortet Paperless mit 406.
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self.server_version: str | None = None
        self.api_version: str | None = None

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params) -> dict:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"GET {path}: {exc}") from exc
        # Paperless meldet bei authentifizierten Anfragen Server- und API-Version
        self.server_version = response.headers.get("x-version", self.server_version)
        self.api_version = response.headers.get("x-api-version", self.api_version)
        return response.json()

    def _paginate(self, path: str, **params) -> Iterator[dict]:
        page = 1
        while True:
            data = self._get(path, page=page, **params)
            yield from data.get("results", [])
            if not data.get("next"):
                return
            page += 1

    def ping(self) -> dict:
        self._get("/api/documents/", page_size=1)
        return {"version": self.server_version, "api_version": self.api_version}

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
                      "page_count,original_file_name,custom_fields",
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

    def custom_field_definitions(self) -> list[dict]:
        return list(self._paginate("/api/custom_fields/", page_size=500))

    def create_custom_field(self, name: str, data_type: str) -> dict:
        try:
            response = self._client.post("/api/custom_fields/", json={"name": name, "data_type": data_type})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Custom Field {name!r} anlegen: {exc}") from exc
        return response.json()

    def set_custom_fields(self, doc_id: int, values: dict[int, object]) -> None:
        """Setzt Felder (Feld-ID -> bereits kodierter Wert, None = leeren).

        Andere Custom Fields des Dokuments bleiben unverändert erhalten.
        """
        doc = self.get_document(doc_id)
        merged = [cf for cf in doc.get("custom_fields", []) if cf["field"] not in values]
        merged += [{"field": field_id, "value": value} for field_id, value in values.items()]
        try:
            response = self._client.patch(f"/api/documents/{doc_id}/", json={"custom_fields": merged})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Dokument {doc_id} aktualisieren: {exc}") from exc
