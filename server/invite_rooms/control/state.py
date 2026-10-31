"""Durable owner-approved loopback link bindings for the RoomLink adapter."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping


class LinkStateError(ValueError):
    pass


class FixedLinkStore:
    """Small atomic JSON store; request data can never create or alter a route."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "links": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinkStateError("link state is unreadable") from exc
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("links"), dict):
            raise LinkStateError("link state schema is invalid")
        return value

    def bind(self, link_id: str, *, target_url: str, scope: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(link_id, str) or not link_id or len(link_id) > 128 or "/" in link_id:
            raise LinkStateError("invalid link id")
        if not isinstance(target_url, str) or not target_url.startswith("http://127.0.0.1:") and not target_url.startswith("http://[::1]:"):
            raise LinkStateError("target URL must be loopback HTTP")
        if not isinstance(scope, Mapping) or not scope:
            raise LinkStateError("fixed scope is required")
        record = {"link_id": link_id, "target_url": target_url, "scope": dict(scope)}
        with self._lock:
            data = self._read()
            old = data["links"].get(link_id)
            if old is not None and old != record:
                raise LinkStateError("approved link binding conflicts with persisted state")
            data["links"][link_id] = record
            self._write(data)
        return record

    def get(self, link_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._read()["links"].get(link_id)
            return dict(value) if isinstance(value, dict) else None

    def all(self) -> tuple[dict[str, Any], ...]:
        """Return persisted owner-approved bindings for explicit adapter restore."""
        with self._lock:
            links = self._read()["links"]
            return tuple(dict(record) for record in links.values() if isinstance(record, dict))

    def _write(self, data: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
