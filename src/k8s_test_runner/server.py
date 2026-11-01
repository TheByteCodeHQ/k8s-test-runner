"""Entry point: ``ktr-server --config suites.yaml``."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from .api import create_app
from .config import ConfigError, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ktr-server", description="Run test suites as isolated Kubernetes Jobs.")
    parser.add_argument("--config", help="Path to suites.yaml (or set KTR_CONFIG)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        app = create_app(settings, api_token=os.getenv("KTR_API_TOKEN") or None,
                         allow_no_auth=os.getenv("KTR_ALLOW_NO_AUTH") == "1")
    except (ConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
