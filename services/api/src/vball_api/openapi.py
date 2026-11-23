"""Write the OpenAPI schema to a file (used to generate the TypeScript client).

Usage: ``uv run vball-api-openapi apps/web/openapi.json``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import SecretStr

from vball_api.main import create_app
from vball_core.settings import Settings


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    # The schema does not depend on runtime configuration; a dummy URL avoids needing secrets.
    settings = Settings(database_url=SecretStr("postgresql+psycopg://u:p@localhost:5432/x"))
    schema = create_app(settings).openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")  # LF on every OS, so CI diffs match
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
