"""Export or check the public contract without starting infrastructure."""

import argparse
import json
from pathlib import Path

from collective.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "contracts/openapi.json"
    expected = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not target.is_file() or target.read_text() != expected:
            raise SystemExit("OpenAPI drift: run pnpm contracts:generate")
        print("OpenAPI contract is current")
    else:
        target.parent.mkdir(exist_ok=True)
        target.write_text(expected)
        print(f"Exported {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
