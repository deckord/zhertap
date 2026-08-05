from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local genplan workbench")
    parser.add_argument("--root", type=Path, required=True, help="Allowed data root")
    parser.add_argument("--manifest", type=Path, required=True, help="Asset manifest JSON")
    parser.add_argument("--output", type=Path, help="Output directory below --root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    app = create_app(
        data_root=arguments.root,
        manifest_path=arguments.manifest,
        output_path=arguments.output,
    )
    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
