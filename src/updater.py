"""Background updater that keeps the icon set in sync with Mojang releases."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import builder, mojang, state as state_mod, uploader
from .config import Settings

logger = logging.getLogger(__name__)


def _human_interval(seconds: int) -> str:
    """Format a duration for log messages.

    Args:
        seconds: Interval length in seconds.

    Returns:
        A short human readable form such as ``6h`` or ``90s``.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class Updater:
    """Owns the HTTP client, the current state and the refresh loop."""

    def __init__(self, settings: Settings):
        """Initialize the updater.

        Args:
            settings: Service settings.
        """
        self.settings = settings
        self.state = state_mod.load(settings.state_file)
        self._client = httpx.AsyncClient(follow_redirects=True)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._busy = False

    async def close(self) -> None:
        """Stop the refresh loop and close the HTTP client."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    async def ensure_ready(self) -> None:
        """Check for a new version on startup and build when needed.

        A usable build is not assumed to be current: one manifest request tells
        us whether Mojang released something newer while the service was down.
        """
        if not self.settings.update_on_start:
            logger.info("startup update disabled, serving whatever is on disk")
            return
        await self.update()

    async def update(
        self,
        version: str | None = None,
        force: bool = False,
        limit: int | None = None,
        upload: bool | None = None,
    ) -> dict:
        """Resolve a version and rebuild the icon set when needed.

        Args:
            version: Explicit version selector, defaults to the configured one.
            force: Rebuild even when the version is already built.
            limit: Build only the first N ids, useful for smoke tests.
            upload: Force (``True``) or skip (``False``) the image bed upload;
                ``None`` follows ``UPLOAD_AFTER_BUILD``.

        Returns:
            A JSON-friendly summary of the operation.
        """
        async with self._lock:
            if self._busy:
                return {"status": "busy"}
            self._busy = True
            try:
                selector = version or self.settings.mc_version
                manifest = await mojang.fetch_manifest(self._client)
                target = mojang.pick_version(manifest, selector)

                if not force and self.state and self.state.version == target.id:
                    if self.state.is_usable():
                        logger.info("already on %s, nothing to build", target.id)
                        summary = {"status": "up-to-date", "version": target.id}
                        if upload:
                            await self._maybe_upload(summary, True)
                        return summary
                if not force and self.state and not self.state.is_usable():
                    logger.warning("cached build directory is gone, rebuilding")

                logger.info("building icons for Minecraft %s (%s)", target.id, target.type)
                meta = await mojang.fetch_version_meta(self._client, target)
                jar_path = await mojang.ensure_client_jar(
                    self._client, target, meta, self.settings.cache_dir
                )
                result = await builder.build(
                    self.settings, target.id, jar_path, self._client, limit=limit
                )

                if limit:
                    logger.warning("partial build (limit=%d), state not updated", limit)
                    return {
                        "status": "partial",
                        "version": result.version,
                        "count": result.count,
                        "from_wiki": result.from_wiki,
                        "from_jar": result.from_jar,
                        "missing": len(result.missing),
                    }

                new_state = state_mod.State(
                    version=result.version,
                    directory=str(result.directory),
                    built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    count=result.count,
                    from_wiki=result.from_wiki,
                    from_jar=result.from_jar,
                    missing=len(result.missing),
                )
                state_mod.save(self.settings.state_file, new_state)
                self.state = new_state
                self._cleanup_old_builds(result.version)

                summary = {
                    "status": "built",
                    "version": result.version,
                    "count": result.count,
                    "from_wiki": result.from_wiki,
                    "from_jar": result.from_jar,
                    "missing": len(result.missing),
                }
                await self._maybe_upload(summary, upload)
                return summary
            finally:
                self._busy = False

    async def _maybe_upload(self, summary: dict, upload: bool | None) -> None:
        """Run the image bed upload when configured and requested.

        Args:
            summary: Result dict of the current run, extended in place.
            upload: ``True`` forces the upload, ``False`` skips it, ``None``
                follows ``UPLOAD_AFTER_BUILD``.
        """
        wanted = self.settings.upload.after_build if upload is None else upload
        if not wanted:
            return
        if not self.settings.upload.enabled:
            logger.warning("upload requested but UPLOAD_BASE_URL/credentials are missing")
            summary["upload"] = {"status": "skipped", "reason": "not configured"}
            return
        report = await uploader.upload_icons(self.settings, client=self._client)
        summary["upload"] = report.as_dict()

    def _cleanup_old_builds(self, keep: str) -> None:
        """Delete icon directories of previous versions.

        Args:
            keep: Version directory to preserve.
        """
        for path in self.settings.data_dir.glob("icons-*"):
            if path.name in {f"icons-{keep}", f"icons-{keep}.tmp"}:
                continue
            logger.info("removing stale build %s", path.name)
            try:
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
            except OSError as exc:
                logger.warning("cannot remove %s: %s", path, exc)

    async def run_forever(self) -> None:
        """Check for new releases on a fixed interval."""
        while True:
            try:
                await asyncio.sleep(self.settings.check_interval)
                logger.info(
                    "scheduled check for a new Minecraft release (every %s)",
                    _human_interval(self.settings.check_interval),
                )
                started = time.monotonic()
                result = await self.update()
                logger.info(
                    "scheduled check finished in %.1fs: %s %s",
                    time.monotonic() - started,
                    result.get("status"),
                    result.get("version", ""),
                )
            except asyncio.CancelledError:
                logger.info("refresh loop stopped")
                raise
            except Exception as exc:  # keep the loop alive on transient failures
                logger.exception("scheduled update failed: %s", exc)

    def start(self) -> None:
        """Start the periodic refresh loop."""
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever())
            logger.info(
                "refresh loop started, next check in %s",
                _human_interval(self.settings.check_interval),
            )
