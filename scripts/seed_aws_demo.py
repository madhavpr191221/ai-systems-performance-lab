"""Generate and seed a reproducible synthetic DynamoDB/S3 dataset.

AWS writes are disabled unless ``--apply`` is supplied. Repeating a run with the
same seed overwrites the same keys with the same records.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from ai_systems_performance_lab.agent.synthetic_data import (
    DEFAULT_SYNTHETIC_CASE_COUNT,
    DEFAULT_SYNTHETIC_SEED,
    generate_synthetic_records,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_KEY = "policies/synthetic-review-v1.txt"


def load_dataset(
    *, generated_count: int, seed: int
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Combine the unchanged hand-written examples with generated records."""
    golden_cases = json.loads(
        (ROOT / "fixtures" / "cases.json").read_text(encoding="utf-8")
    )
    cases = list(golden_cases)
    objects: dict[str, bytes] = {}
    for case in golden_cases:
        evidence_path = ROOT / "fixtures" / "evidence" / f"{case['case_id']}.json"
        objects[f"cases/{case['case_id']}/evidence.json"] = evidence_path.read_bytes()

    for record in generate_synthetic_records(generated_count, seed):
        case = record["case"]
        cases.append(case)
        key = f"cases/{case['case_id']}/evidence.json"
        objects[key] = json.dumps(
            record["evidence"], indent=2, ensure_ascii=False
        ).encode("utf-8")

    objects[POLICY_KEY] = (
        ROOT / "fixtures" / "synthetic-review-policy-v1.txt"
    ).read_bytes()
    return cases, objects


def dynamodb_item(case: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        "case_id": {"S": case["case_id"]},
        "annual_revenue": {"N": str(case["annual_revenue"])},
        "operating_income": {"N": str(case["operating_income"])},
        "annual_debt_service": {"N": str(case["annual_debt_service"])},
        "complexity": {"S": case["complexity"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="AWS profile (SDK default if omitted)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--generated-count", type=int, default=DEFAULT_SYNTHETIC_CASE_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SYNTHETIC_SEED)
    parser.add_argument("--s3-workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true", help="perform writes; default is dry-run")
    args = parser.parse_args()
    if args.generated_count < 0:
        parser.error("--generated-count must be non-negative")
    if not 1 <= args.s3_workers <= 32:
        parser.error("--s3-workers must be between 1 and 32")

    cases, objects = load_dataset(
        generated_count=args.generated_count, seed=args.seed
    )
    print(
        f"Plan: {len(cases)} DynamoDB records and {len(objects)} S3 objects "
        f"({args.generated_count} generated; seed {args.seed}) in {args.region}."
    )
    print(f"Table: {args.table}; bucket: {args.bucket}.")
    if not args.apply:
        print("Dry run only: no AWS API writes were made. Re-run with --apply to write.")
        return 0

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)
    with table.batch_writer(overwrite_by_pkeys=["case_id"]) as batch:
        for case in cases:
            batch.put_item(Item=case)

    s3 = session.client(
        "s3", config=Config(max_pool_connections=args.s3_workers)
    )

    def put_object(key: str, body: bytes) -> None:
        content_type = (
            "application/json" if key.endswith(".json") else "text/plain; charset=utf-8"
        )
        s3.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    with ThreadPoolExecutor(max_workers=args.s3_workers) as executor:
        futures = [executor.submit(put_object, key, body) for key, body in objects.items()]
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            if completed % 100 == 0 or completed == len(futures):
                print(f"Uploaded {completed}/{len(futures)} S3 objects.", flush=True)
    print("Synthetic dataset upload complete. No credentials or case contents were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
