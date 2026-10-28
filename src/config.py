"""Configuration loaded from a local ``.env`` file and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs from a dotenv file into ``os.environ``.

    Existing environment variables win, so ``FOO=1 python main.py`` still
    overrides the file. Missing files are ignored.

    Args:
        path: Dotenv path, defaults to ``$ENV_FILE`` or ``<project>/.env``.
    """
    env_path = path or Path(os.getenv("ENV_FILE", PROJECT_ROOT / ".env"))
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_bool(key: str, default: bool) -> bool:
    """Read a boolean environment variable.

    Args:
        key: Variable name.
        default: Value used when the variable is unset.

    Returns:
        The parsed boolean.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    """Read an integer environment variable, falling back on bad input.

    Args:
        key: Variable name.
        default: Value used when the variable is unset or invalid.

    Returns:
        The parsed integer.
    """
    try:
        return int(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


@dataclass(slots=True)
class UploadSettings:
    """Parameters of the image bed upload API (CloudFlare ImgBed ``POST /upload``).

    Every field maps to one request parameter, so the whole call can be shaped
    from ``.env`` without touching code.

    Attributes:
        base_url: Image bed origin, e.g. ``https://cfbed.sanyue.de``.
        path: Upload endpoint path, ``/upload`` for plain multipart upload.
        auth_code: ``authCode`` query parameter.
        token: API token sent as ``Authorization: Bearer``.
        channel: ``uploadChannel``, one of telegram/cfr2/s3/discord/huggingface/webdav.
        channel_name: ``channelName``, picks a named channel in multi-channel setups.
        folder: ``uploadFolder``, target directory inside the bed.
        name_type: ``uploadNameType``, ``origin`` keeps the original file name.
        return_format: ``returnFormat``, ``default`` returns ``/file/<id>``.
        server_compress: ``serverCompress``, only affects images on the Telegram channel.
        auto_retry: ``autoRetry``, lets the server switch channels on failure.
        concurrency: Number of parallel uploads.
        timeout: Per-request timeout in seconds.
        skip_existing: Skip files already reachable under ``public_base``.
        public_base: Public URL prefix used for the existence probe, e.g.
            ``https://img.xcstar.top/file``.
        after_build: Upload automatically after every successful build.
        extra_params: Extra query parameters, for options added by the bed later.
    """

    base_url: str = field(default_factory=lambda: os.getenv("UPLOAD_BASE_URL", "").rstrip("/"))
    path: str = field(default_factory=lambda: os.getenv("UPLOAD_PATH", "/upload"))
    auth_code: str = field(default_factory=lambda: os.getenv("UPLOAD_AUTH_CODE", ""))
    token: str = field(default_factory=lambda: os.getenv("UPLOAD_TOKEN", ""))
    channel: str = field(default_factory=lambda: os.getenv("UPLOAD_CHANNEL", "telegram"))
    channel_name: str = field(default_factory=lambda: os.getenv("UPLOAD_CHANNEL_NAME", ""))
    folder: str = field(default_factory=lambda: os.getenv("UPLOAD_FOLDER", "mc_admin"))
    name_type: str = field(default_factory=lambda: os.getenv("UPLOAD_NAME_TYPE", "origin"))
    return_format: str = field(default_factory=lambda: os.getenv("UPLOAD_RETURN_FORMAT", "default"))
    server_compress: bool = field(default_factory=lambda: _env_bool("UPLOAD_SERVER_COMPRESS", False))
    auto_retry: bool = field(default_factory=lambda: _env_bool("UPLOAD_AUTO_RETRY", True))
    concurrency: int = field(default_factory=lambda: _env_int("UPLOAD_CONCURRENCY", 2))
    timeout: int = field(default_factory=lambda: _env_int("UPLOAD_TIMEOUT", 60))
    skip_existing: bool = field(default_factory=lambda: _env_bool("UPLOAD_SKIP_EXISTING", True))
    public_base: str = field(
        default_factory=lambda: os.getenv("UPLOAD_PUBLIC_BASE", "").rstrip("/")
    )
    after_build: bool = field(default_factory=lambda: _env_bool("UPLOAD_AFTER_BUILD", False))
    extra_params: dict[str, str] = field(
        default_factory=lambda: dict(
            item.split("=", 1)
            for item in os.getenv("UPLOAD_EXTRA_PARAMS", "").split(",")
            if "=" in item
        )
    )

    @property
    def enabled(self) -> bool:
        """Whether a base URL and at least one credential are configured."""
        return bool(self.base_url and (self.auth_code or self.token))

    @property
    def endpoint(self) -> str:
        """Absolute upload endpoint URL."""
        return f"{self.base_url}{self.path}"

    def query_params(self) -> dict[str, str]:
        """Build the query string of a plain upload request.

        Empty values are dropped so the bed falls back to its own defaults.

        Returns:
            Query parameters in the order documented by the API.
        """
        params = {
            "authCode": self.auth_code,
            "uploadChannel": self.channel,
            "channelName": self.channel_name,
            "uploadFolder": self.folder.strip("/"),
            "uploadNameType": self.name_type,
            "returnFormat": self.return_format,
            "serverCompress": "true" if self.server_compress else "false",
            "autoRetry": "true" if self.auto_retry else "false",
        }
        params.update(self.extra_params)
        return {key: value for key, value in params.items() if value}

    def headers(self) -> dict[str, str]:
        """Build request headers, including the bearer token when configured.

        Returns:
            Headers to send with every upload.
        """
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def masked_params(self) -> dict[str, str]:
        """Return the query parameters with credentials hidden.

        Returns:
            A copy safe for logs and reports.
        """
        return {
            key: ("***" if key.lower() in {"authcode", "token", "apikey"} else value)
            for key, value in self.query_params().items()
        }


@dataclass(slots=True)
class Settings:
    """Runtime settings for the icon service.

    Attributes:
        data_dir: Directory that stores cached jars, wiki lookups and built icons.
        host: Bind address for the HTTP server.
        port: Bind port for the HTTP server.
        mc_version: Version selector, either ``latest``, ``latest-snapshot`` or an id.
        sources: Ordered icon sources, ``wiki`` renders first, ``jar`` textures second.
        icon_size: Square canvas size in pixels every icon is normalised to.
        check_interval: Seconds between two automatic version checks.
        update_on_start: Rebuild on startup when the cached build is missing or stale.
        refresh_token: When set, ``POST /refresh`` requires a matching ``X-Token`` header.
        concurrency: Maximum number of parallel upstream requests.
        log_level: Console log level, ``INFO`` by default, ``DEBUG`` for detail.
        upload: Image bed upload parameters.
    """

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data")))
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8618")))
    mc_version: str = field(default_factory=lambda: os.getenv("MC_VERSION", "latest"))
    sources: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().lower() for s in os.getenv("SOURCES", "wiki,jar").split(",") if s.strip()
        )
    )
    icon_size: int = field(default_factory=lambda: int(os.getenv("ICON_SIZE", "96")))
    check_interval: int = field(default_factory=lambda: int(os.getenv("CHECK_INTERVAL", "21600")))
    update_on_start: bool = field(default_factory=lambda: _env_bool("UPDATE_ON_START", True))
    refresh_token: str | None = field(default_factory=lambda: os.getenv("REFRESH_TOKEN") or None)
    concurrency: int = field(default_factory=lambda: int(os.getenv("CONCURRENCY", "4")))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    upload: UploadSettings = field(default_factory=UploadSettings)

    @property
    def cache_dir(self) -> Path:
        """Directory holding downloaded client jars and wiki lookup cache."""
        return self.data_dir / "cache"

    @property
    def state_file(self) -> Path:
        """Path of the JSON file describing the active build."""
        return self.data_dir / "state.json"


load_env_file()
