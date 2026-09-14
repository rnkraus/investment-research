"""Export or verify the deterministic FastAPI OpenAPI contract."""

import argparse
import json
from pathlib import Path

from investment_research.main import create_app
from investment_research.settings import ApiSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "frontend/src/lib/api/generated/openapi.json"


def render_openapi() -> str:
    """Render a stable JSON representation of the application contract."""

    schema = create_app(ApiSettings(environment="test")).openapi()
    return f"{json.dumps(schema, indent=2, sort_keys=True)}\n"


def main() -> int:
    """Write the schema or fail when the committed schema is stale."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    rendered = render_openapi()
    if arguments.check:
        if not arguments.output.is_file() or arguments.output.read_text() != rendered:
            parser.error("the committed OpenAPI contract is missing or out of date")
        return 0

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
