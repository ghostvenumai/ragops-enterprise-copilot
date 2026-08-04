"""File-backed synthetic CRM and document store."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ragops.ingestion.pipeline import ingest_directory
from ragops.storage.models import Contract, Customer, DocumentChunk, SupportTicket


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class JsonRepository:
    """Local repository backed by synthetic files.

    The name is retained for future JSON persistence; current CRM fixtures are
    CSV to keep them inspectable in portfolio demos.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.document_dir = data_dir / "documents"
        self.crm_dir = data_dir / "crm"
        self._chunks: list[DocumentChunk] | None = None
        self._customers: list[Customer] | None = None
        self._contracts: list[Contract] | None = None
        self._tickets: list[SupportTicket] | None = None

    def chunks(self) -> list[DocumentChunk]:
        if self._chunks is None:
            self._chunks = ingest_directory(self.document_dir)
        return list(self._chunks)

    def customers(self) -> list[Customer]:
        if self._customers is None:
            self._customers = [
                Customer(
                    customer_id=row["customer_id"],
                    tenant_id=row["tenant_id"],
                    name=row["name"],
                    segment=row["segment"],
                    owner=row["owner"],
                    region=row["region"],
                )
                for row in _read_csv(self.crm_dir / "customers.csv")
            ]
        return list(self._customers)

    def contracts(self) -> list[Contract]:
        if self._contracts is None:
            self._contracts = [
                Contract(
                    contract_id=row["contract_id"],
                    tenant_id=row["tenant_id"],
                    customer_id=row["customer_id"],
                    product=row["product"],
                    value_eur=int(row["value_eur"]),
                    end_date=date.fromisoformat(row["end_date"]),
                    status=row["status"],
                    document_id=row["document_id"],
                )
                for row in _read_csv(self.crm_dir / "contracts.csv")
            ]
        return list(self._contracts)

    def tickets(self) -> list[SupportTicket]:
        if self._tickets is None:
            self._tickets = [
                SupportTicket(
                    ticket_id=row["ticket_id"],
                    tenant_id=row["tenant_id"],
                    customer_id=row["customer_id"],
                    severity=row["severity"],
                    status=row["status"],
                    title=row["title"],
                    opened_at=date.fromisoformat(row["opened_at"]),
                    document_id=row["document_id"],
                )
                for row in _read_csv(self.crm_dir / "tickets.csv")
            ]
        return list(self._tickets)
