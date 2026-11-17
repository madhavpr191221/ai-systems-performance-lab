"""Read-only repositories used by the agent's bounded AWS tools."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ai_systems_performance_lab.agent.workload import CreditCase


class CaseNotFoundError(LookupError):
    """The requested synthetic case is not present in the case store."""


class CaseRepository(Protocol):
    def get_case(self, case_id: str) -> CreditCase: ...


def _case_from_item(item: Mapping[str, Any]) -> CreditCase:
    try:
        return CreditCase(
            case_id=str(item["case_id"]),
            annual_revenue=float(item["annual_revenue"]),
            operating_income=float(item["operating_income"]),
            annual_debt_service=float(item["annual_debt_service"]),
            complexity=str(item.get("complexity", "standard")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Stored case record is missing valid required fields") from exc


class InMemoryCaseRepository:
    """Deterministic data source for offline UI, tests, and load fixtures."""

    def __init__(self, cases: Mapping[str, CreditCase]) -> None:
        self.cases = dict(cases)

    def get_case(self, case_id: str) -> CreditCase:
        try:
            return self.cases[case_id]
        except KeyError as exc:
            raise CaseNotFoundError(case_id) from exc


class DynamoDBCaseRepository:
    """Fetch one case by exact key; it never scans or writes the table."""

    def __init__(
        self,
        *,
        table_name: str,
        region_name: str,
        client: Any | None = None,
    ) -> None:
        self.table_name = table_name
        self.client = client or boto3.Session(region_name=region_name).client(
            "dynamodb",
            config=Config(
                connect_timeout=2, read_timeout=4, retries={"max_attempts": 2}
            ),
        )

    def get_case(self, case_id: str) -> CreditCase:
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"case_id": {"S": case_id}},
                ConsistentRead=True,
            )
        except ClientError as exc:
            raise RuntimeError("Case store is unavailable") from exc
        item = response.get("Item")
        if not item:
            raise CaseNotFoundError(case_id)
        return _case_from_item(_deserialize_item(item))


def _deserialize_item(value: Mapping[str, Any]) -> dict[str, Any]:
    """Decode DynamoDB low-level attribute values without boto resource globals."""

    def decode(item: Any) -> Any:
        if isinstance(item, dict):
            if "S" in item:
                return item["S"]
            if "N" in item:
                return Decimal(item["N"])
            if "BOOL" in item:
                return item["BOOL"]
            if "M" in item:
                return _deserialize_item(item["M"])
            if "L" in item:
                return [decode(child) for child in item["L"]]
        return item

    return {key: decode(item) for key, item in value.items()}


class S3EvidenceReader:
    """Read one case-bound JSON evidence object and return bounded excerpts."""

    def __init__(
        self,
        *,
        bucket: str,
        region_name: str,
        prefix: str = "cases",
        max_bytes: int = 65_536,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.max_bytes = max_bytes
        self.client = client or boto3.Session(region_name=region_name).client(
            "s3",
            config=Config(
                connect_timeout=2, read_timeout=5, retries={"max_attempts": 2}
            ),
        )

    def search(self, *, case_id: str, query: str) -> list[dict[str, str]]:
        key = f"{self.prefix}/{case_id}/evidence.json"
        document = _read_json_object(
            self.client,
            bucket=self.bucket,
            key=key,
            max_bytes=self.max_bytes,
        )
        entries = document.get("evidence", [])
        if not isinstance(entries, list):
            raise TypeError("Evidence document has an invalid evidence list")
        terms = {term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", query)}
        ranked: list[tuple[int, dict[str, str]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = {
                "id": str(entry.get("id", "unknown"))[:80],
                "text": str(entry.get("text", ""))[:1200],
            }
            corpus = f"{item['id']} {item['text']}".casefold()
            score = sum(term in corpus for term in terms)
            if score or not terms:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [item for _, item in ranked[:3]]


class S3PolicyReader:
    """Read a single configured policy object; key is never model-controlled."""

    def __init__(
        self,
        *,
        bucket: str,
        key: str,
        region_name: str,
        max_bytes: int = 32_768,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.key = key
        self.max_bytes = max_bytes
        self.client = client or boto3.Session(region_name=region_name).client(
            "s3",
            config=Config(
                connect_timeout=2, read_timeout=5, retries={"max_attempts": 2}
            ),
        )

    def read_policy(self) -> str:
        body = _read_object(
            self.client, bucket=self.bucket, key=self.key, max_bytes=self.max_bytes
        )
        return body.decode("utf-8")


def _read_json_object(
    client: Any, *, bucket: str, key: str, max_bytes: int
) -> dict[str, Any]:
    try:
        payload = _read_object(client, bucket=bucket, key=key, max_bytes=max_bytes)
    except ClientError as exc:
        raise RuntimeError("Evidence store is unavailable") from exc
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Evidence object is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise TypeError("Evidence object must contain a JSON object")
    return document


def _read_object(client: Any, *, bucket: str, key: str, max_bytes: int) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        payload = body.read(max_bytes + 1)
    finally:
        body.close()
    if len(payload) > max_bytes:
        raise ValueError(f"S3 object exceeds {max_bytes} bytes")
    return payload
