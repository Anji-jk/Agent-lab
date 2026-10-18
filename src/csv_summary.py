import argparse
import csv
import json
import sys
from pathlib import Path


def summarize_csv(file_path: Path) -> dict:
    """Return a summary of a CSV file."""
    try:
        with file_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            reader = csv.DictReader(file)

            if reader.fieldnames is None:
                raise ValueError("CSV file is empty or has no header.")

            columns = reader.fieldnames
            row_count = 0
            missing_values = {column: 0 for column in columns}

            for row in reader:
                row_count += 1

                for column in columns:
                    value = row.get(column)
                    if value is None or not value.strip():
                        missing_values[column] += 1

            return {
                "file": str(file_path),
                "rows": row_count,
                "columns": len(columns),
                "column_names": columns,
                "missing_values": missing_values,
            }

    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Could not read CSV file: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize one or more CSV files."
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="CSV files to summarize.",
    )
    args = parser.parse_args()

    failed = False

    for file_path in args.files:
        try:
            result = summarize_csv(file_path)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except ValueError as exc:
            failed = True
            print(
                f"ERROR: {file_path}: {exc}",
                file=sys.stderr,
            )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())