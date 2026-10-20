"""Schlanker Client für die Paperless-NGX-REST-API."""

from collections.abc import Iterator
from datetime import date

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
