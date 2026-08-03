"""Optional Emby library refresh integration after a media file is repaired."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class EmbyItemNotReady(RuntimeError):
    """The repaired path is not present as exactly one Emby item yet."""


def _secret() -> str:
    direct = os.getenv("EMBY_API_KEY", "").strip()
    filename = os.getenv("EMBY_API_KEY_FILE", "").strip()
    if direct and filename:
        raise RuntimeError("Set only one of EMBY_API_KEY or EMBY_API_KEY_FILE")
    if filename:
        try:
            return Path(filename).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Unable to read EMBY_API_KEY_FILE") from exc
    return direct


@dataclass(frozen=True)
class EmbyRefreshClient:
    base_url: str
    api_key: str
    local_media_root: Path
    server_media_root: PurePosixPath
    timeout_seconds: int = 60

    @classmethod
    def from_environment(cls, local_media_root: Path) -> "EmbyRefreshClient | None":
        base_url = os.getenv("EMBY_URL", "").strip()
        api_key = _secret()
        server_root = os.getenv("EMBY_MEDIA_ROOT", "").strip()
        supplied = (bool(base_url), bool(api_key), bool(server_root))
        if not any(supplied):
            return None
        if not all(supplied):
            raise RuntimeError(
                "EMBY_URL, EMBY_MEDIA_ROOT and one API key setting are all required"
            )
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            local_media_root=local_media_root.resolve(),
            server_media_root=PurePosixPath(server_root),
            timeout_seconds=max(5, int(os.getenv("EMBY_TIMEOUT_SECONDS", "60"))),
        )

    def server_path(self, media_path: Path) -> str:
        try:
            relative = media_path.resolve().relative_to(self.local_media_root)
        except ValueError as exc:
            raise RuntimeError("Repaired path is outside MEDIA_ROOT") from exc
        return str(self.server_media_root.joinpath(*relative.parts))

    def _request(
        self,
        endpoint: str,
        parameters: dict[str, str],
        *,
        method: str = "GET",
    ) -> dict[str, Any] | None:
        url = self.base_url + "/" + endpoint.lstrip("/")
        if parameters:
            url += "?" + urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            url,
            data=b"" if method == "POST" else None,
            method=method,
            headers={"X-Emby-Token": self.api_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    @staticmethod
    def _normalized_path(value: str) -> str:
        return str(PurePosixPath(value.replace("\\", "/")))

    def refresh(self, media_path: Path) -> str:
        expected = self.server_path(media_path)
        response = self._request("Items", {
            "Recursive": "true",
            "Path": expected,
            "Fields": "Path",
            "IncludeItemTypes": "Movie,Video",
            "Limit": "10",
        })
        items = (response or {}).get("Items", [])
        exact = [
            item for item in items
            if self._normalized_path(str(item.get("Path", ""))) == self._normalized_path(expected)
        ]
        if len(exact) != 1:
            raise EmbyItemNotReady(
                "Repaired path is not present as exactly one media-library item"
            )
        item_id = str(exact[0].get("Id", "")).strip()
        if not item_id:
            raise EmbyItemNotReady("Matched media-library item has no identifier")
        self._request(f"Items/{urllib.parse.quote(item_id, safe='')}/Refresh", {
            "Recursive": "true",
            "MetadataRefreshMode": "FullRefresh",
            "ImageRefreshMode": "FullRefresh",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
            "ReplaceThumbnailImages": "true",
        }, method="POST")
        return item_id
