"""Validate a supplied client-search CSV without importing it or collecting data."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clinic_geo.outreach.api import (  # noqa: E402
    SuppliedDirectoryValidationError,
    load_supplied_clinic_directory,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="User-supplied UTF-8 CSV")
    args = parser.parse_args(argv)
    try:
        directory = load_supplied_clinic_directory(args.file)
    except SuppliedDirectoryValidationError as exc:
        print(json.dumps({
            "status": "invalid",
            "issues": [asdict(issue) for issue in exc.issues],
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "status": "valid",
        "schema_version": directory.schema_version,
        "country_code": directory.country_code,
        "city": directory.city,
        "accounts": len(directory.accounts),
        "branches": len(directory.branches),
        "duplicate_rows": directory.duplicate_rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
