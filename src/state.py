"""Persisted build state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class State:
    """Description of the icon set currently served.

    Attributes:
        version: Minecraft version of the build.
        directory: Absolute path of the icon directory.
        built_at: ISO timestamp of the build.
        count: Number of icons in the build.
        from_wiki: Icons sourced from the wiki.
        from_jar: Icons sourced from the client jar.
        missing: Number of ids that no source could satisfy.
    """

    version: str
    directory: str
    built_at: str
    count: int
    from_wiki: int
    from_jar: int
    missing: int

    def icons_dir(self) -> Path:
        """Return the icon directory as a path."""
        return Path(self.directory)

    def is_usable(self) -> bool:
        """Return whether the referenced directory still holds an index."""
        return (self.icons_dir() / "index.json").is_file()


def load(path: Path) -> State | None:
    """Load the state file.

    Args:
        path: Path of ``state.json``.

    Returns:
        The stored state, or ``None`` when missing or unreadable.
    """
    try:
        return State(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save(path: Path, state: State) -> None:
    """Write the state file atomically.

    Args:
        path: Path of ``state.json``.
        state: State to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
