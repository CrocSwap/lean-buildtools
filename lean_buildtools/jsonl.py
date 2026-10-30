"""Read logs that can end halfway through a record after a compiler is stopped."""
import json
from pathlib import Path


def read_rows(path: Path, *, missing_ok=False) -> list[dict]:
    try:
        lines = path.read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            if number == len(lines) and not line.endswith(b"\n"):
                break
            raise ValueError(f"malformed JSON record in {path}:{number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"expected JSON object in {path}:{number}")
        rows.append(row)
    return rows
