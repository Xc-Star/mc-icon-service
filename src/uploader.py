"""Upload the built icon set to the CloudFlare ImgBed instance."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import state as state_mod
from .config import Settings

logger = logging.getLogger(__name__)

MIME_BY_SUFFIX = {".png": "image/png", ".gif": "image/gif"}


@dataclass(slots=True)
class UploadReport:
    """Outcome of one upload run.

    Attributes:
        total: Number of icon files considered.
        uploaded: Number of files pushed to the bed.
        skipped: Number of files already present on the bed.
        failed: Failures as ``(file name, reason)`` pairs.
        report_path: Where the JSON report was written.
    """

    total: int
    uploaded: int
    skipped: int
    failed: list[tuple[str, str]] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    report_path: Path | None = None

    def as_dict(self) -> dict:
        """Return a JSON-friendly summary."""
        return {
            "total": self.total,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "failed": len(self.failed),
            "name_mismatch": len(self.mismatched),
            "report": str(self.report_path) if self.report_path else None,
        }


def _public_url(settings: Settings, name: str) -> str:
    """Build the public URL of an icon on the bed.

    Args:
        settings: Service settings.
        name: Icon file name.

    Returns:
        Absolute URL, or an empty string when no public base is configured.
    """
    upload = settings.upload
    if not upload.public_base:
        return ""
    folder = upload.folder.strip("/")
    return f"{upload.public_base}/{folder}/{name}" if folder else f"{upload.public_base}/{name}"


def _parse_response(payload: object) -> tuple[str, str]:
    """Extract the stored path and public URL from an upload response.

    Args:
        payload: Decoded JSON body.

    Returns:
        ``(src, public_url)``, both empty when the bed reported a failure.

    Raises:
        ValueError: When the payload is not a recognisable upload result.
    """
    if isinstance(payload, list) and payload:
        item = payload[0]
        if isinstance(item, dict):
            return str(item.get("src", "")), str(item.get("publicUrl", ""))
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise ValueError(str(payload.get("message") or payload.get("error") or "rejected"))
        return str(payload.get("src", "")), str(payload.get("publicUrl", ""))
    raise ValueError(f"unexpected response: {payload!r}"[:200])


async def upload_icons(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> UploadReport:
    """Push every icon of the active build to the image bed.

    Args:
        settings: Service settings, including the upload parameters.
        client: Optional shared HTTP client.
        limit: Upload only the first N files, useful for smoke tests.
        force: Upload even when the file already exists on the bed.
        dry_run: Print the request that would be sent and upload nothing.

    Returns:
        Summary of the run.

    Raises:
        RuntimeError: When no build exists or the upload is not configured.
    """
    state = state_mod.load(settings.state_file)
    if state is None or not state.is_usable():
        raise RuntimeError("no icon build yet, run `python main.py update` first")

    upload = settings.upload
    if not upload.enabled and not dry_run:
        raise RuntimeError("upload is not configured, set UPLOAD_BASE_URL and UPLOAD_AUTH_CODE")

    files = sorted(
        path
        for path in state.icons_dir().iterdir()
        if path.suffix in MIME_BY_SUFFIX and path.name.startswith("minecraft_")
    )
    if limit:
        files = files[:limit]

    params = upload.query_params()
    headers = upload.headers()
    logger.info("uploading %d icons to %s", len(files), upload.endpoint)
    logger.info("query params: %s", upload.masked_params())
    logger.info("folder: %s, name type: %s", upload.folder or "(root)", upload.name_type)

    if dry_run:
        for path in files[:5]:
            logger.info(
                "would POST %s as file=%s (%s, %d bytes)",
                upload.endpoint,
                path.name,
                MIME_BY_SUFFIX[path.suffix],
                path.stat().st_size,
            )
        return UploadReport(total=len(files), uploaded=0, skipped=0)

    owned_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=True)
    semaphore = asyncio.Semaphore(max(1, upload.concurrency))
    report = UploadReport(total=len(files), uploaded=0, skipped=0)

    async def one(path: Path) -> None:
        async with semaphore:
            if upload.skip_existing and not force:
                url = _public_url(settings, path.name)
                if url:
                    try:
                        probe = await http.head(url, timeout=upload.timeout)
                        if probe.status_code == 200:
                            report.skipped += 1
                            return
                    except httpx.HTTPError:
                        pass

            data = path.read_bytes()
            for attempt in range(2):
                try:
                    response = await http.post(
                        upload.endpoint,
                        params=params,
                        headers=headers,
                        files={"file": (path.name, data, MIME_BY_SUFFIX[path.suffix])},
                        timeout=upload.timeout,
                    )
                    response.raise_for_status()
                    src, _ = _parse_response(response.json())
                    if src and not src.endswith(f"/{path.name}"):
                        if len(report.mismatched) < 5:
                            report.mismatched.append(f"{path.name} -> {src}")
                    report.uploaded += 1
                    return
                except (httpx.HTTPError, ValueError) as exc:
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    report.failed.append((path.name, str(exc)))

    try:
        for index, task in enumerate(asyncio.as_completed([one(path) for path in files]), start=1):
            await task
            if index % 100 == 0 or index == len(files):
                logger.info(
                    "progress %d/%d (uploaded %d, skipped %d, failed %d)",
                    index,
                    len(files),
                    report.uploaded,
                    report.skipped,
                    len(report.failed),
                )
    finally:
        if owned_client:
            await http.aclose()

    report.report_path = settings.data_dir / "upload-report.json"
    report.report_path.write_text(
        json.dumps(
            {
                "version": state.version,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "endpoint": upload.endpoint,
                "folder": upload.folder,
                "params": upload.masked_params(),
                **report.as_dict(),
                "failures": [{"file": name, "reason": reason} for name, reason in report.failed],
                "name_mismatch_samples": report.mismatched,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "upload done: %d uploaded, %d skipped, %d failed",
        report.uploaded,
        report.skipped,
        len(report.failed),
    )
    return report
