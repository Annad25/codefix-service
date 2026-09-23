"""Run the service:  python -m app  [--host 0.0.0.0] [--port 8000]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import create_app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
