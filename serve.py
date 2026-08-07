"""Custom Chainlit launcher that bypasses nest_asyncio.

Chainlit's CLI calls ``nest_asyncio.apply()`` at import time, which patches
asyncio internals and breaks ``sniffio.current_async_library()`` on
Python 3.14 — Starlette's ``FileResponse`` then raises
``anyio.NoEventLoopError`` for every static asset, leaving the UI blank.

This launcher performs the same setup as ``chainlit run`` without touching
nest_asyncio, then hands off to plain ``uvicorn.run``.

Usage:  python serve.py           (defaults to app.py on 127.0.0.1:8000)
        python serve.py app.py --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="app.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not target.is_file():
        sys.exit(f"Target file not found: {target}")

    # Ensure the target's directory is on sys.path so relative imports work.
    sys.path.insert(0, str(target.parent))

    # Import chainlit machinery *after* setting sys.path, and skip the CLI
    # module entirely (that's the one that calls nest_asyncio.apply()).
    from chainlit.auth import ensure_jwt_secret
    from chainlit.cache import init_lc_cache
    from chainlit.config import config, load_module
    from chainlit.markdown import init_markdown
    from chainlit.server import app

    config.run.host = args.host
    config.run.port = args.port
    config.run.root_path = os.environ.get("CHAINLIT_ROOT_PATH", "")
    config.run.module_name = str(target)

    load_module(str(target))
    ensure_jwt_secret()
    init_markdown(config.root)
    init_lc_cache()

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.debug else "info",
        ws="auto",
    )


if __name__ == "__main__":
    main()
