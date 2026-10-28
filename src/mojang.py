"""Mojang version manifest helpers: resolve a version and cache its client jar."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "mc-icon-service/1.0 (+https://github.com/xc-star)"
# Fail fast instead of hanging when the host is unreachable.
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class VersionInfo:
    """Resolved Minecraft version.

    Attributes:
        id: Version id such as ``26.3``.
        type: ``release`` or ``snapshot``.
        meta_url: URL of the per-version metadata document.
    """

    id: str
    type: str
    meta_url: str


def manifest_url(mirror: str = "") -> str:
    """Build the version manifest URL.

    Args:
        mirror: Optional mirror base such as ``https://bmclapi2.bangbang93.com``.

    Returns:
        The manifest URL to fetch.
    """
    return f"{mirror}/mc/game/version_manifest_v2.json" if mirror else MANIFEST_URL


async def fetch_manifest(client: httpx.AsyncClient, mirror: str = "") -> dict:
    """Fetch the version manifest.

    Args:
        client: Shared HTTP client.
        mirror: Optional mirror base, see :func:`manifest_url`.

    Returns:
        The decoded manifest document.

    Raises:
        httpx.HTTPError: When the host is unreachable or returns an error.
    """
    url = manifest_url(mirror)
    logger.info("fetching version manifest from %s", url)
    response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def pick_version(manifest: dict, selector: str, mirror: str = "") -> VersionInfo:
    """Pick a version from the manifest.

    Args:
        manifest: Manifest document returned by :func:`fetch_manifest`.
        selector: ``latest``, ``latest-snapshot`` or an explicit version id.
        mirror: Optional mirror base; when set the per-version document is read
            from the mirror instead of Mojang.

    Returns:
        The matching version.

    Raises:
        ValueError: When the selector matches nothing.
    """
    latest = manifest.get("latest", {})
    wanted = {
        "latest": latest.get("release"),
        "latest-release": latest.get("release"),
        "latest-snapshot": latest.get("snapshot"),
    }.get(selector.lower(), selector)

    for entry in manifest.get("versions", []):
        if entry["id"] == wanted:
            meta_url = (
                f"{mirror}/version/{entry['id']}/json" if mirror else entry["url"]
            )
            return VersionInfo(id=entry["id"], type=entry["type"], meta_url=meta_url)
    raise ValueError(f"unknown Minecraft version: {selector}")


async def fetch_version_meta(client: httpx.AsyncClient, version: VersionInfo) -> dict:
    """Fetch the per-version metadata document.

    Args:
        client: Shared HTTP client.
        version: Version resolved by :func:`pick_version`.

    Returns:
        The decoded version document, which contains download URLs.
    """
    logger.info("fetching version metadata for %s", version.id)
    response = await client.get(
        version.meta_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )
    response.raise_for_status()
    return response.json()


async def ensure_client_jar(
    client: httpx.AsyncClient,
    version: VersionInfo,
    meta: dict,
    cache_dir: Path,
    mirror: str = "",
) -> Path:
    """Download the client jar once and reuse it afterwards.

    The jar is the authoritative source for every vanilla texture and for the
    complete block/item id list (``blockstates/*.json`` and ``items/*.json``).

    Args:
        client: Shared HTTP client.
        version: Version being built.
        meta: Version document returned by :func:`fetch_version_meta`.
        cache_dir: Directory that keeps jars between runs.
        mirror: Optional mirror base used for the jar download.

    Returns:
        Path of the cached jar.

    Raises:
        ValueError: When the version document has no client download.
        RuntimeError: When the downloaded file fails its sha1 check.
    """
    download = meta.get("downloads", {}).get("client")
    if not download:
        raise ValueError(f"version {version.id} has no client download")

    cache_dir.mkdir(parents=True, exist_ok=True)
    jar_path = cache_dir / f"client-{version.id}.jar"

    if jar_path.exists() and _sha1(jar_path) == download["sha1"]:
        logger.info("client jar cache hit: %s", jar_path.name)
        return jar_path

    jar_url = f"{mirror}/version/{version.id}/client" if mirror else download["url"]
    logger.info(
        "downloading client jar %s (%.1f MB) from %s",
        version.id,
        download["size"] / 1048576,
        jar_url,
    )
    tmp_path = jar_path.with_suffix(".part")
    async with client.stream(
        "GET",
        jar_url,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(300.0, connect=15.0),
    ) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as handle:
            async for chunk in response.aiter_bytes(1 << 16):
                handle.write(chunk)

    if _sha1(tmp_path) != download["sha1"]:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"sha1 mismatch for client jar {version.id}")

    tmp_path.replace(jar_path)
    return jar_path


def _sha1(path: Path) -> str:
    """Return the sha1 hex digest of a file."""
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
