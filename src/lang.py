"""Localized item names from the vanilla language file.

The client jar only ships ``en_us``; every other locale lives in the asset
index as a separate object on ``resources.download.minecraft.net``.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

ASSET_HOST = "https://resources.download.minecraft.net"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def fetch_language(
    client: httpx.AsyncClient, meta: dict, lang_code: str, mirror: str = ""
) -> dict[str, str]:
    """Download one language file for a version.

    Args:
        client: Shared HTTP client.
        meta: Version document, which points at the asset index.
        lang_code: Locale such as ``zh_cn``.
        mirror: Optional mirror base used for the asset object.

    Returns:
        The raw translation table, empty when it cannot be fetched.
    """
    index_url = (meta.get("assetIndex") or {}).get("url")
    if not index_url:
        logger.warning("version document has no assetIndex, cannot read item names")
        return {}

    try:
        index = (await client.get(index_url, timeout=TIMEOUT)).json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("cannot read asset index: %s", exc)
        return {}

    entry = (index.get("objects") or {}).get(f"minecraft/lang/{lang_code}.json")
    if not entry:
        logger.warning("locale %s is not present in the asset index", lang_code)
        return {}

    digest = entry["hash"]
    candidates = [f"{ASSET_HOST}/{digest[:2]}/{digest}"]
    if mirror:
        candidates.insert(0, f"{mirror}/assets/{digest[:2]}/{digest}")

    for url in candidates:
        try:
            response = await client.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            logger.info("loaded %s language file (%d entries)", lang_code, len(response.json()))
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("cannot download language file from %s: %s", url, exc)

    logger.warning("could not download the %s language file", lang_code)
    return {}


def build_mapping(language: dict[str, str], ids: set[str]) -> dict[str, str]:
    """Build an id to display name map for the given ids.

    Double slabs are added as ``<id>[type=double]`` so block state ids parsed
    from litematic files still resolve to a readable name.

    Args:
        language: Translation table keyed by ``block.minecraft.<id>`` etc.
        ids: Plain ids such as ``acacia_stairs``.

    Returns:
        Mapping of ``minecraft:<id>`` to display name, sorted by key.
    """
    names: dict[str, str] = {}
    for item_id in sorted(ids):
        # `<key>.new` holds the full display name for items whose base name is
        # generic (music discs, banner patterns, smithing templates).
        # `<key>.desc` is the parenthesised part, e.g. "C418 - cat".
        name = ""
        for prefix in ("block.minecraft.", "item.minecraft."):
            key = f"{prefix}{item_id}"
            if key not in language:
                continue
            name = language.get(f"{key}.new") or language[key]
            desc = language.get(f"{key}.desc")
            if desc and f"{key}.new" not in language:
                name = f"{name}（{desc}）"
            break
        if not name:
            continue
        names[f"minecraft:{item_id}"] = name
        if item_id.endswith("_slab"):
            names[f"minecraft:{item_id}[type=double]"] = f"双层{name}"
    return names
