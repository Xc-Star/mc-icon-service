"""HTTP API serving the generated icon set."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings
from .updater import Updater

logger = logging.getLogger(__name__)

settings = Settings()

QUIET_PATHS = {"/healthz", "/readyz"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare the updater, warm the cache in the background and start polling."""
    upload = settings.upload
    logger.info("starting mc-icon-service, data dir: %s", settings.data_dir.resolve())
    logger.info(
        "sources=%s icon_size=%d concurrency=%d check_interval=%ss",
        ",".join(settings.sources),
        settings.icon_size,
        settings.concurrency,
        settings.check_interval,
    )
    logger.info(
        "upload: %s -> %s (channel=%s, channel_name=%s, folder=%s, name_type=%s, skip_existing=%s)",
        "enabled" if upload.enabled else "disabled",
        upload.endpoint,
        upload.channel or "-",
        upload.channel_name or "-",
        upload.folder or "-",
        upload.name_type,
        upload.skip_existing,
    )
    if upload.enabled and not upload.public_base:
        logger.warning("UPLOAD_PUBLIC_BASE is empty, every upload run will re-send all files")

    updater = Updater(settings)
    app.state.updater = updater
    if updater.state and updater.state.is_usable():
        logger.info(
            "loaded build %s: %d icons (%d wiki, %d jar), %d missing, built at %s, dir %s",
            updater.state.version,
            updater.state.count,
            updater.state.from_wiki,
            updater.state.from_jar,
            updater.state.missing,
            updater.state.built_at,
            Path(updater.state.directory).resolve(),
        )
    else:
        logger.info("no usable build on disk, a full build will start now")

    app.state.warmup = asyncio.create_task(updater.ensure_ready())
    updater.start()
    try:
        yield
    finally:
        logger.info("shutting down mc-icon-service")
        await updater.close()


app = FastAPI(title="Minecraft icon service", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """Log every request with status and duration.

    Health probes are logged at debug level so they do not drown the log.

    Args:
        request: Incoming request.
        call_next: Downstream handler.

    Returns:
        The response produced by the handler.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        logger.exception(
            "%s %s from %s failed after %.1fms",
            request.method,
            request.url.path,
            request.client.host if request.client else "-",
            elapsed,
        )
        raise

    elapsed = (time.perf_counter() - started) * 1000
    level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
    size = response.headers.get("content-length", "-")
    logger.log(
        level,
        "%s %s%s -> %d in %.1fms, %s bytes (%s)",
        request.method,
        request.url.path,
        f"?{request.url.query}" if request.url.query else "",
        response.status_code,
        elapsed,
        size,
        request.client.host if request.client else "-",
    )
    return response


def _state(request: Request):
    """Return the current state or raise 503 when nothing is built yet.

    Args:
        request: Incoming request, used to reach the app state.

    Returns:
        The active build state.

    Raises:
        HTTPException: When no usable build exists.
    """
    state = request.app.state.updater.state
    if state is None or not state.is_usable():
        raise HTTPException(status_code=503, detail="icon set is not ready yet")
    return state


@app.get("/")
async def root() -> dict:
    """Describe the service and its endpoints."""
    return {
        "service": "mc-icon-service",
        "endpoints": ["/healthz", "/version", "/map", "/icons/{name}", "/refresh"],
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe that also reports whether icons are available."""
    state = app.state.updater.state
    return {"ok": True, "ready": bool(state and state.is_usable())}


@app.get("/readyz")
async def readyz(request: Request) -> dict:
    """Readiness probe: 503 until the first build is available.

    Args:
        request: Incoming request, used to reach the app state.

    Returns:
        ``{"ok": true}`` once icons can be served.

    Raises:
        HTTPException: 503 while the first build is still running.
    """
    _state(request)
    return {"ok": True}


@app.get("/version")
async def version(request: Request) -> dict:
    """Report the Minecraft version of the active build."""
    state = _state(request)
    return {
        "version": state.version,
        "built_at": state.built_at,
        "count": state.count,
        "from_wiki": state.from_wiki,
        "from_jar": state.from_jar,
        "missing": state.missing,
    }


@app.get("/map")
async def icon_map(request: Request) -> JSONResponse:
    """Return the ``name -> extension`` map used by the AstrBot plugin."""
    state = _state(request)
    path = state.icons_dir() / "material_icon_map.json"
    if not path.is_file():
        raise HTTPException(status_code=503, detail="map is not ready yet")
    return JSONResponse(content=json.loads(path.read_text(encoding="utf-8")))


@app.get("/icons/{name}")
async def icon(request: Request, name: str) -> FileResponse:
    """Serve one icon.

    Args:
        request: Incoming request.
        name: File name such as ``minecraft_stone.png``; ``stone`` also works.

    Returns:
        The icon file with caching headers.
    """
    state = _state(request)
    directory = state.icons_dir()

    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid name")

    if "." not in name:
        base = name if name.startswith("minecraft_") else f"minecraft_{name}"
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        extension = index["icons"].get(base)
        if not extension:
            raise HTTPException(status_code=404, detail=f"unknown icon: {name}")
        name = f"{base}{extension}"

    path = directory / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown icon: {name}")

    stat = path.stat()
    return FileResponse(
        path,
        headers={
            "Cache-Control": "public, max-age=604800",
            "ETag": f'"{stat.st_mtime_ns}-{stat.st_size}"',
        },
    )


@app.post("/refresh")
async def refresh(request: Request, version: str | None = None, force: bool = False) -> dict:
    """Trigger an update.

    Args:
        request: Incoming request, used to check the optional token.
        version: Explicit version selector, defaults to the configured one.
        force: Rebuild even when the version is already built.

    Returns:
        The updater result.

    Raises:
        HTTPException: When the refresh token is configured and does not match.
    """
    if settings.refresh_token and request.headers.get("x-token") != settings.refresh_token:
        raise HTTPException(status_code=401, detail="invalid token")
    logger.info("manual refresh requested (version=%s, force=%s)", version or "default", force)
    return await request.app.state.updater.update(version=version, force=force)
