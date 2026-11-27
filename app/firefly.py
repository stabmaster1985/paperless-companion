"""Minimaler Client für die Firefly-III-API (nur Lesen von Buchungen)."""

from collections.abc import Iterator
from dataclasses import dataclass, field
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
    category: str = ""  # Merkmale aus Firefly, um Buchungen eines Vertrags wiederzuerkennen
    tags: list[str] = field(default_factory=list)
    bill: str = ""  # Firefly-„Abo“ (Bill / Dauerauftrag-Erkennung in Firefly)
    budget: str = ""

    @property
    def marks(self) -> list[tuple[str, str]]:
        """Firefly-Merkmale als (Art, Wert) für Anzeige und Regeln."""
        out = [("category", self.category)] if self.category else []
        out += [("tag", t) for t in self.tags]
        out += [("bill", self.bill)] if self.bill else []
        out += [("budget", self.budget)] if self.budget else []
        return out


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

    MARK_ENDPOINTS = {"category": ("categories", "name"), "tag": ("tags", "tag"), "bill": ("bills", "name"),
                      "budget": ("budgets", "name")}

    def names(self, mark: str) -> list[str]:
        """Namen der Kategorien, Tags, Firefly-Abos (Bills) bzw. Budgets – für Auswahllisten (schnell, ohne Buchungen)."""
        endpoint, attr = self.MARK_ENDPOINTS[mark]
        out, page = [], 1
        while page <= 20:
            try:
                response = self._client.get(f"/api/v1/{endpoint}", params={"page": page, "limit": 100})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise FireflyError(f"{endpoint} laden: {exc}") from exc
            data = response.json()
            out += [str(x.get("attributes", {}).get(attr) or "") for x in data.get("data", [])]
            if page >= int(data.get("meta", {}).get("pagination", {}).get("total_pages", 1) or 1):
                break
            page += 1
        return sorted({n for n in out if n}, key=str.lower)

    def withdrawals(self, start: date, end: date) -> Iterator[Transaction]:
        return self._transactions("withdrawal", start, end)

    def deposits(self, start: date, end: date) -> Iterator[Transaction]:
        """Geldeingänge (z.B. Erstattungen der Versicherung); Gegenseite ist hier der Absender."""
        return self._transactions("deposit", start, end)

    def _transactions(self, kind: str, start: date, end: date) -> Iterator[Transaction]:
        page = 1
        while True:
            try:
                response = self._client.get("/api/v1/transactions", params={
                    "type": kind, "start": start.isoformat(), "end": end.isoformat(), "page": page, "limit": 100,
                })
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise FireflyError(f"Buchungen laden: {exc}") from exc
            data = response.json()
            for group in data.get("data", []):
                for split in group.get("attributes", {}).get("transactions", []):
                    yield _parse(group["id"], split, incoming=kind == "deposit")
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= int(pagination.get("total_pages", 1) or 1):
                return
            page += 1


def _parse(group_id: str, split: dict, incoming: bool = False) -> Transaction:
    text_parts = [split.get(k) or "" for k in ("description", "notes", "internal_reference", "external_id", "sepa_ct_id",
                                                "sepa_db", "sepa_ep", "sepa_ci")]
    return Transaction(
        journal_id=str(split.get("transaction_journal_id") or group_id),
        group_id=str(group_id),
        date=date.fromisoformat(str(split["date"])[:10]),
        amount=round(abs(float(split["amount"])), 2),
        counterparty=split.get("source_name" if incoming else "destination_name") or "",
        iban=(split.get("source_iban" if incoming else "destination_iban") or "").replace(" ", "").upper() or None,
        description=split.get("description") or "",
        text=" ".join(text_parts),
        category=split.get("category_name") or "",
        tags=[str(t) for t in (split.get("tags") or []) if t],
        bill=split.get("bill_name") or split.get("subscription_name") or "",
        budget=split.get("budget_name") or "",
    )
