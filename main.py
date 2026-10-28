"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from src import builder, mojang, uploader
from src.config import Settings
from src.updater import Updater


def _configure_logging(level: str) -> None:
    """Set up console logging.

    Args:
        level: Log level name, e.g. ``INFO`` or ``DEBUG``.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # route uvicorn's own lifecycle messages through the root handler so every
    # line shares the same timestamp and logger prefix
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


async def _serve(settings: Settings) -> None:
    """Run the HTTP service.

    Args:
        settings: Service settings.
    """
    import uvicorn

    await uvicorn.Server(
        uvicorn.Config(
            "src.server:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            # keep our own formatter for uvicorn's lifecycle messages
            log_config=None,
            # requests are logged by our own middleware with status + duration
            access_log=False,
        )
    ).serve()


async def _update(
    settings: Settings, version: str | None, force: bool, limit: int | None, upload: bool
) -> None:
    """Run a single update.

    Args:
        settings: Service settings.
        version: Version selector override.
        force: Rebuild even when already built.
        limit: Build only the first N ids.
        upload: Upload to the image bed after the build.
    """
    updater = Updater(settings)
    try:
        result = await updater.update(
            version=version, force=force, limit=limit, upload=True if upload else None
        )
    finally:
        await updater.close()
    print(result)


async def _upload(
    settings: Settings, limit: int | None, force: bool, dry_run: bool
) -> None:
    """Push the current build to the image bed.

    Args:
        settings: Service settings.
        limit: Upload only the first N files.
        force: Upload even when the file already exists.
        dry_run: Print the request instead of sending files.
    """
    try:
        report = await uploader.upload_icons(settings, limit=limit, force=force, dry_run=dry_run)
    except RuntimeError as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(report.as_dict())


async def _list_versions(limit: int) -> None:
    """Print the most recent Minecraft versions.

    Args:
        limit: How many entries to print.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        manifest = await mojang.fetch_manifest(client)
    print(f"latest release:  {manifest['latest']['release']}")
    print(f"latest snapshot: {manifest['latest']['snapshot']}")
    for entry in manifest["versions"][:limit]:
        print(f"  {entry['id']:<24} {entry['type']}")


def _info(settings: Settings) -> None:
    """Print the active build state.

    Args:
        settings: Service settings.
    """
    from src import state as state_mod

    state = state_mod.load(settings.state_file)
    if not state:
        print("no build yet, run `python main.py update` first")
        return
    print(f"version:   {state.version}")
    print(f"built at:  {state.built_at}")
    print(f"icons:     {state.count} (wiki {state.from_wiki}, jar {state.from_jar})")
    print(f"missing:   {state.missing}")
    print(f"directory: {state.directory}")
    print(f"usable:    {state.is_usable()}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch.

    Args:
        argv: Argument list, defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Minecraft icon update service")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP service")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    update = sub.add_parser("update", help="build the icon set once")
    update.add_argument("--version", default=None, help="latest, latest-snapshot or a version id")
    update.add_argument("--force", action="store_true", help="rebuild even if up to date")
    update.add_argument("--limit", type=int, default=None, help="only build the first N ids")
    update.add_argument("--upload", action="store_true", help="upload to the image bed afterwards")

    upload = sub.add_parser("upload", help="push the current build to the image bed")
    upload.add_argument("--limit", type=int, default=None, help="only upload the first N files")
    upload.add_argument("--force", action="store_true", help="upload even if already on the bed")
    upload.add_argument("--dry-run", action="store_true", help="print the request, upload nothing")

    versions = sub.add_parser("list-versions", help="print recent Minecraft versions")
    versions.add_argument("--limit", type=int, default=15)

    sub.add_parser("info", help="show the active build")

    args = parser.parse_args(argv)
    settings = Settings()
    _configure_logging("DEBUG" if args.verbose else settings.log_level)

    if args.command == "serve":
        if args.host:
            settings.host = args.host
        if args.port:
            settings.port = args.port
        asyncio.run(_serve(settings))
    elif args.command == "update":
        asyncio.run(_update(settings, args.version, args.force, args.limit, args.upload))
    elif args.command == "upload":
        asyncio.run(_upload(settings, args.limit, args.force, args.dry_run))
    elif args.command == "list-versions":
        asyncio.run(_list_versions(args.limit))
    elif args.command == "info":
        _info(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
