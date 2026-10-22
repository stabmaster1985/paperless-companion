"""Minimaler Client für die Firefly-III-API (nur Lesen von Buchungen)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import httpx


class FireflyError(RuntimeError):
    pass


@dataclass
class Transaction:
    journal_id: str
    group_id: str
    date: date
    amount: float
    counterparty: str
    iban: str | None
    description: str
    text: str  # Beschreibung + Notizen + Referenzen für die Suche nach Rechnungsnummern


class FireflyClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def ping(self) -> dict:
        try:
            response = self._client.get("/api/v1/about")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FireflyError(str(exc)) from exc
        return response.json().get("data", {})

    def own_ibans(self) -> list[str]:
        """IBANs deiner eigenen Konten (Bestandskonten) – die sind nie ein Merkmal eines Absenders."""
        try:
            response = self._client.get("/api/v1/accounts", params={"type": "asset", "limit": 200})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FireflyError(f"Konten laden: {exc}") from exc
        return sorted({(a.get("attributes", {}).get("iban") or "").replace(" ", "").upper()
                       for a in response.json().get("data", [])} - {""})

    def link(self, group_id: str) -> str:
        return f"{self.base_url}/transactions/show/{group_id}"

    def withdrawals(self, start: date, end: date) -> Iterator[Transaction]:
        page = 1
        while True:
            try:
                response = self._client.get("/api/v1/transactions", params={
                    "type": "withdrawal", "start": start.isoformat(), "end": end.isoformat(), "page": page, "limit": 100,
                })
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise FireflyError(f"Buchungen laden: {exc}") from exc
            data = response.json()
            for group in data.get("data", []):
                for split in group.get("attributes", {}).get("transactions", []):
                    yield _parse(group["id"], split)
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= int(pagination.get("total_pages", 1) or 1):
                return
            page += 1


def _parse(group_id: str, split: dict) -> Transaction:
    text_parts = [split.get(k) or "" for k in ("description", "notes", "internal_reference", "external_id", "sepa_ct_id",
                                                "sepa_db", "sepa_ep", "sepa_ci")]
    return Transaction(
        journal_id=str(split.get("transaction_journal_id") or group_id),
        group_id=str(group_id),
        date=date.fromisoformat(str(split["date"])[:10]),
        amount=round(abs(float(split["amount"])), 2),
        counterparty=split.get("destination_name") or "",
        iban=(split.get("destination_iban") or "").replace(" ", "").upper() or None,
        description=split.get("description") or "",
        text=" ".join(text_parts),
    )
