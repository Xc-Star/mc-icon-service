"""Build the icon set from a client jar plus wiki renders."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

from . import lang
from .config import Settings
from .wiki import WikiClient

logger = logging.getLogger(__name__)

TEXTURE_ROOT = "assets/minecraft/textures"
ID_ROOTS = {
    "assets/minecraft/blockstates/": "block",
    "assets/minecraft/items/": "item",
}


@dataclass(slots=True)
class BuildResult:
    """Outcome of one build.

    Attributes:
        version: Minecraft version the icons belong to.
        directory: Directory holding the generated icons.
        count: Number of icons written.
        from_wiki: How many icons came from the wiki.
        from_jar: How many icons came from the client jar.
        missing: Ids that no source could satisfy.
    """

    version: str
    directory: Path
    count: int
    from_wiki: int
    from_jar: int
    missing: list[str] = field(default_factory=list)


def collect_ids(jar_path: Path) -> list[str]:
    """List every block and item id shipped by a client jar.

    Args:
        jar_path: Path of the client jar.

    Returns:
        Sorted ids such as ``acacia_stairs`` or ``diamond_sword``.
    """
    ids: set[str] = set()
    with zipfile.ZipFile(jar_path) as jar:
        for name in jar.namelist():
            for root in ID_ROOTS:
                if name.startswith(root) and name.endswith(".json"):
                    ids.add(name[len(root) : -len(".json")])
    return sorted(ids)


def _texture_of(jar: zipfile.ZipFile, block_id: str) -> bytes | None:
    """Read the fallback texture of an id from the jar.

    Args:
        jar: Opened client jar.
        block_id: Id such as ``stone``.

    Returns:
        PNG bytes, or ``None`` when the jar has no matching texture.
    """
    for folder in ("item", "block"):
        try:
            return jar.read(f"{TEXTURE_ROOT}/{folder}/{block_id}.png")
        except KeyError:
            continue
    return None


def _fit(image: Image.Image, size: int) -> Image.Image:
    """Center an image on a transparent square canvas.

    Upscaling keeps hard pixel edges, downscaling uses a smooth filter so wiki
    renders do not alias.

    Args:
        image: Source image in RGBA mode.
        size: Canvas edge length in pixels.

    Returns:
        The squared canvas.
    """
    ratio = min(size / image.width, size / image.height)
    target = (
        max(1, round(image.width * ratio)),
        max(1, round(image.height * ratio)),
    )
    resample = Image.Resampling.NEAREST if ratio >= 1 else Image.Resampling.LANCZOS
    scaled = image.resize(target, resample)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(scaled, ((size - target[0]) // 2, (size - target[1]) // 2), scaled)
    return canvas


def _gif_frame(image: Image.Image, size: int) -> Image.Image:
    """Convert one RGBA frame to a palette frame that keeps transparency.

    GIF only supports a single fully transparent palette index, so index 255 is
    reserved for it and every translucent pixel is mapped onto it.

    Args:
        image: Source frame in RGBA mode.
        size: Canvas edge length in pixels.

    Returns:
        A palette-mode frame ready to be written to a GIF.
    """
    frame = _fit(image, size)
    alpha = frame.getchannel("A")
    palette = frame.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    palette.paste(255, mask=alpha.point(lambda value: 255 if value < 128 else 0))
    return palette


def _normalize(data: bytes, size: int) -> tuple[bytes, str] | None:
    """Normalise an upstream image to a square PNG or animated GIF.

    Args:
        data: Raw image bytes.
        size: Canvas edge length in pixels.

    Returns:
        ``(payload, extension)``, or ``None`` when the image cannot be decoded.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > 1:
                rendered: list[Image.Image] = []
                durations: list[int] = []
                for index in range(frames):
                    image.seek(index)
                    rendered.append(_gif_frame(image.convert("RGBA"), size))
                    durations.append(int(image.info.get("duration", 100)) or 100)
                buffer = io.BytesIO()
                rendered[0].save(
                    buffer,
                    format="GIF",
                    save_all=True,
                    append_images=rendered[1:],
                    duration=durations,
                    loop=0,
                    disposal=2,
                    transparency=255,
                )
                return buffer.getvalue(), ".gif"

            buffer = io.BytesIO()
            _fit(image.convert("RGBA"), size).save(buffer, format="PNG")
            return buffer.getvalue(), ".png"
    except (OSError, ValueError) as exc:
        logger.debug("cannot decode image: %s", exc)
        return None


async def build(
    settings: Settings,
    version: str,
    jar_path: Path,
    client: httpx.AsyncClient,
    limit: int | None = None,
    meta: dict | None = None,
) -> BuildResult:
    """Generate a complete icon set for one Minecraft version.

    Each id is resolved through the configured sources in order. The wiki
    provides isometric renders (including composite blocks), the jar provides
    flat textures as a fallback.

    Args:
        settings: Service settings.
        version: Minecraft version id.
        jar_path: Path of the client jar.
        client: Shared HTTP client.
        limit: Build only the first N ids, useful for smoke tests.
        meta: Version document, needed to locate the language file.

    Returns:
        Summary of the build.
    """
    ids = collect_ids(jar_path)
    if limit:
        ids = ids[:limit]

    # Icons are written in place: rebuilding the same version overwrites files
    # instead of copying the whole set around, and a new version gets a new dir.
    target = settings.data_dir / f"icons-{version}"
    target.mkdir(parents=True, exist_ok=True)

    wiki = WikiClient(client, settings.cache_dir) if "wiki" in settings.sources else None
    semaphore = asyncio.Semaphore(max(1, settings.concurrency))
    icons: dict[str, str] = {}
    missing: list[str] = []
    stats = {"wiki": 0, "jar": 0}
    started = time.monotonic()

    with zipfile.ZipFile(jar_path) as jar:

        async def one(block_id: str) -> None:
            async with semaphore:
                render = await wiki.resolve(block_id) if wiki else None
                if render:
                    payload = None
                    for attempt in range(2):
                        try:
                            payload = await wiki.download(render.url)
                            break
                        except httpx.HTTPError as exc:
                            logger.debug("download failed for %s: %s", block_id, exc)
                            await asyncio.sleep(0.5 * (attempt + 1))
                    normalized = _normalize(payload, settings.icon_size) if payload else None
                    if normalized:
                        body, ext = normalized
                        (target / f"minecraft_{block_id}{ext}").write_bytes(body)
                        icons[f"minecraft_{block_id}"] = ext
                        stats["wiki"] += 1
                        return

                raw = _texture_of(jar, block_id)
                normalized = _normalize(raw, settings.icon_size) if raw else None
                if normalized is None:
                    missing.append(block_id)
                    return
                body, ext = normalized
                (target / f"minecraft_{block_id}{ext}").write_bytes(body)
                icons[f"minecraft_{block_id}"] = ext
                stats["jar"] += 1

        tasks = [asyncio.create_task(one(block_id)) for block_id in ids]
        total = len(tasks)
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            await task
            if index % 100 == 0 or index == total:
                logger.info(
                    "progress %d/%d (wiki %d, jar %d, missing %d)",
                    index,
                    total,
                    stats["wiki"],
                    stats["jar"],
                    len(missing),
                )

    item_count = 0
    if settings.emit_item_mapping and meta and not limit:
        try:
            language = await lang.fetch_language(
                client, meta, settings.lang_code, settings.mirror_url
            )
            mapping = lang.build_mapping(
                language, {name.removeprefix("minecraft_") for name in icons}
            )
            if mapping:
                (target / "item_mapping.json").write_text(
                    json.dumps(
                        {
                            "version": version,
                            "description": (
                                f"Minecraft 物品/方块 ID 与名称映射（{settings.lang_code}），"
                                "由 mc-icon-service 自动生成"
                            ),
                            "items": mapping,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                item_count = len(mapping)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            logger.warning("item_mapping.json skipped: %s", exc)

    index_payload = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(icons),
        "icon_size": settings.icon_size,
        "items": item_count,
        "sources": stats,
        "icons": dict(sorted(icons.items())),
    }
    (target / "index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target / "material_icon_map.json").write_text(
        json.dumps(dict(sorted(icons.items())), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    written = {f"{name}{ext}" for name, ext in icons.items()}
    if not limit:
        for path in target.iterdir():
            if path.is_file() and path.suffix in {".png", ".gif"} and path.name not in written:
                path.unlink(missing_ok=True)

    logger.info(
        "build %s done in %.1fs: %d icons (wiki %d, jar %d), %d missing",
        version,
        time.monotonic() - started,
        len(icons),
        stats["wiki"],
        stats["jar"],
        len(missing),
    )
    return BuildResult(
        version=version,
        directory=target,
        count=len(icons),
        from_wiki=stats["wiki"],
        from_jar=stats["jar"],
        missing=sorted(missing),
    )
