"""Minecraft Wiki render lookup.

The official client jar only ships *textures*. Composite blocks such as stairs,
slabs, fences or doors are assembled from models, so they have no texture of
their own. The wiki keeps one isometric render per block, which is what this
module resolves: ``acacia_stairs`` -> ``Acacia_Stairs_(N)_JE4_BE2.png``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://minecraft.wiki/api.php"
USER_AGENT = "mc-icon-service/1.0 (+https://github.com/xc-star)"

# Files that share an id prefix but are not the block render itself.
JUNK_TOKENS = (
    "mcd",
    "mce",
    "dungeons",
    "legends",
    "story",
    "earth",
    "texture",
    "sprite",
    "grid",
    "breaking",
    "revision",
    "only",
    "icon",
    "ambient",
    "mcc",
    "plastic",
    "candy",
    "preview",
    "screenshot",
    "artwork",
    "comparison",
    "particles",
    "recursion",
    "resolution",
    "_irl",
    "_gui",
    "_ui",
    "reveal",
)

VERSION_TOKEN = re.compile(r"_(?:JE\d+|BE\d+)(?=_|$)")
AGE_TOKEN = re.compile(r"_Age_(\d+)(?:-\d+)?")
SUFFIX_TOKEN = re.compile(r"_\(([A-Za-z]+)\)$")
ORIENTATION_CHARS = set("NSEWUDnsewud")

# A negative lookup expires so a file added to the wiki later can still be found.
NEGATIVE_TTL = 7 * 24 * 3600

# Ranks for the non-orientation suffixes the wiki uses for canonical renders.
STATE_SUFFIX_RANK = {
    "lit": 3,
    "unlit": 3,
    "on": 3,
    "off": 3,
    "top": 1,
    "side": 1,
    "bottom": 1,
    "inventory": 0,
    "item": -1,
}


@dataclass(slots=True)
class Render:
    """A resolved wiki render.

    Attributes:
        name: File name on the wiki, e.g. ``Stone_JE5_BE3.png``.
        url: Absolute download URL.
    """

    name: str
    url: str


def title_of(block_id: str) -> str:
    """Convert an id to the wiki file prefix.

    Args:
        block_id: Id such as ``acacia_stairs``.

    Returns:
        Wiki prefix such as ``Acacia_Stairs``.
    """
    return "_".join(part.capitalize() for part in block_id.split("_"))


def _suffix_rank(token: str) -> int | None:
    """Rank a parenthesised file suffix.

    Args:
        token: Text inside the parentheses, e.g. ``ewU`` or ``lit``.

    Returns:
        A rank where higher is preferred, or ``None`` for unsupported suffixes.
    """
    if token in STATE_SUFFIX_RANK:
        return STATE_SUFFIX_RANK[token]
    if token and all(char in ORIENTATION_CHARS for char in token):
        axes = {char.upper() for char in token}
        if axes == {"U", "D"}:
            return 6
        if "N" in axes:
            return 5
        if "S" in axes:
            return 4
        return 2
    return None


def score(name: str, prefix: str) -> tuple[int, int, int, int, int] | None:
    """Score a wiki file name against an id prefix.

    Args:
        name: Candidate file name, extension included.
        prefix: Expected prefix, e.g. ``Acacia_Stairs``.

    Returns:
        A sortable tuple ``(suffix_rank, growth_stage, je, be, animated)``, or
        ``None`` when the candidate is not a render of that block.
    """
    if not name.startswith(prefix) or not name.endswith((".png", ".gif")):
        return None
    if any(token in name.lower() for token in JUNK_TOKENS):
        return None

    stem, _, ext = name.rpartition(".")

    je = be = 0
    for match in VERSION_TOKEN.finditer(stem):
        token = match.group(0)
        value = int(token[3:])
        if token.startswith("_JE"):
            je = max(je, value)
        else:
            be = max(be, value)
    remainder = VERSION_TOKEN.sub("", stem)

    age = 0
    match = AGE_TOKEN.search(remainder)
    if match:
        age = int(match.group(1))
        remainder = remainder[: match.start()] + remainder[match.end() :]

    rank = 1
    if remainder != prefix:
        match = SUFFIX_TOKEN.search(remainder)
        if not match or remainder[: match.start()] != prefix:
            return None
        rank = _suffix_rank(match.group(1))
        if rank is None:
            return None

    return (rank, age, je, be, 1 if ext == "gif" else 0)


def pick_render(files: list[Render], block_id: str) -> Render | None:
    """Pick the canonical render out of a wiki prefix listing.

    Preference order: upright/facing render over inventory sprite, fully grown
    crop stage, newest JE/BE revision, animated file last.

    Args:
        files: Candidates sharing the block prefix.
        block_id: Block id such as ``acacia_stairs``.

    Returns:
        The best candidate, or ``None`` when nothing usable was listed.
    """
    prefix = title_of(block_id)
    best: tuple[tuple[int, int, int, int, int], Render] | None = None
    for candidate in files:
        value = score(candidate.name, prefix)
        if value is not None and (best is None or value > best[0]):
            best = (value, candidate)
    return best[1] if best else None


class WikiClient:
    """Thin async client for the wiki image API with an on-disk lookup cache."""

    def __init__(self, client: httpx.AsyncClient, cache_dir: Path, interval: float = 0.3):
        """Initialize the client.

        Args:
            client: Shared HTTP client.
            cache_dir: Directory used to cache resolved renders.
            interval: Minimum seconds between two API requests.
        """
        self._client = client
        self._cache_dir = cache_dir / "wiki"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._interval = interval
        self._throttle = asyncio.Lock()
        self._last_call = 0.0

    async def resolve(self, block_id: str) -> Render | None:
        """Resolve the render of one block id, using the cache when possible.

        Args:
            block_id: Block id such as ``acacia_stairs``.

        Returns:
            The resolved render, or ``None`` when the wiki has nothing usable.
        """
        hit, cached = self._read_cache(block_id)
        if hit:
            return cached

        for attempt in range(3):
            files = await self._list_prefix(block_id)
            if files is None:
                logger.debug("retrying %s after transport error", block_id)
                await asyncio.sleep(1 + attempt)
                continue
            render = pick_render(files, block_id)
            if render is None:
                render = await self._page_image(block_id)
            self._write_cache(block_id, render)
            return render

        logger.warning("giving up on %s for now, will retry on the next run", block_id)
        return None

    async def _page_image(self, block_id: str) -> Render | None:
        """Fall back to the wiki page's representative image.

        Some ids do not share their file prefix with the block name, for example
        ``hay_block`` lives on the ``Hay Bale`` page.

        Args:
            block_id: Id such as ``hay_block``.

        Returns:
            The page image, or ``None`` when the page has no usable image.
        """
        title = " ".join(part.capitalize() for part in block_id.split("_"))
        payload = await self._request(
            {
                "action": "query",
                "titles": title,
                "prop": "pageimages",
                "pithumbsize": "300",
                "redirects": "1",
                "format": "json",
            }
        )
        if payload is None:
            return None
        for page in payload.get("query", {}).get("pages", {}).values():
            name = page.get("pageimage") or ""
            url = (page.get("thumbnail") or {}).get("source") or ""
            if not name.endswith((".png", ".gif")) or not url:
                continue
            if any(token in name.lower() for token in JUNK_TOKENS):
                continue
            return Render(name=name, url=url)
        return None

    async def download(self, url: str) -> bytes:
        """Download a render.

        Args:
            url: Absolute file URL returned by the API.

        Returns:
            The raw file bytes.

        Raises:
            httpx.HTTPStatusError: When the wiki rejects the download.
        """
        response = await self._client.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        response.raise_for_status()
        return response.content

    async def _list_prefix(self, block_id: str) -> list[Render] | None:
        """List wiki files sharing the render prefix of a block id.

        Args:
            block_id: Block id such as ``acacia_stairs``.

        Returns:
            Candidate renders, or ``None`` when the API could not be reached.
        """
        params = {
            "action": "query",
            "list": "allimages",
            "aiprefix": title_of(block_id),
            "ailimit": "500",
            "format": "json",
        }
        files: list[Render] = []
        for _ in range(3):
            payload = await self._request(params)
            if payload is None:
                return None
            for image in payload.get("query", {}).get("allimages", []):
                if image.get("url"):
                    files.append(Render(name=image["name"], url=image["url"]))
            cont = payload.get("continue", {}).get("aicontinue")
            if not cont:
                break
            params = {**params, "aicontinue": cont}
        return files

    async def _request(self, params: dict) -> dict | None:
        """Perform one throttled API request.

        Args:
            params: Query parameters.

        Returns:
            Decoded JSON payload, or ``None`` when the request failed.
        """
        async with self._throttle:
            wait = self._interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await self._client.get(
                    API_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=60
                )
                if response.status_code == 429:
                    logger.warning("wiki rate limit hit, backing off")
                    await asyncio.sleep(5)
                    return None
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                logger.debug("wiki request failed: %s", exc)
                return None
            finally:
                self._last_call = time.monotonic()

    def _cache_path(self, block_id: str) -> Path:
        """Return the cache file of one block id."""
        return self._cache_dir / f"{block_id}.json"

    def _read_cache(self, block_id: str) -> tuple[bool, Render | None]:
        """Read a cached lookup result.

        Args:
            block_id: Block id.

        Returns:
            ``(True, render)`` on hit, ``(False, None)`` when a fresh lookup is needed.
        """
        path = self._cache_path(block_id)
        if not path.exists():
            return False, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        name = data.get("name") or ""
        if not name:
            if time.time() - path.stat().st_mtime > NEGATIVE_TTL:
                return False, None
            return True, None
        return True, Render(name=name, url=data.get("url", ""))

    def _write_cache(self, block_id: str, render: Render | None) -> None:
        """Persist a lookup result.

        Args:
            block_id: Block id.
            render: Resolved render, or ``None`` for a negative result.
        """
        payload = {
            "name": render.name if render else "",
            "url": render.url if render else "",
        }
        try:
            self._cache_path(block_id).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("cannot write wiki cache for %s: %s", block_id, exc)
