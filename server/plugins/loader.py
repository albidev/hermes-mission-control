#!/usr/bin/env python3
"""Plugin loader — discovers plugins, loads manifests, dispatches requests.

Architecture:
  1. Each plugin lives in server/plugins/<id>/
  2. Each plugin has a manifest.json describing its endpoints
  3. Each plugin has an endpoints.py exporting handler functions
  4. The loader discovers all plugins at startup and registers them
  5. The telemetry server delegates plugin requests to the loader

The loader is a singleton — call get_loader() from anywhere.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


SERVER_DIR = Path(__file__).resolve().parent.parent
PLUGINS_DIR = Path(__file__).resolve().parent


class PluginHandler:
    """Resolved handler with metadata."""

    def __init__(self, plugin_id: str, method: str, path: str, handler_fn: Callable):
        self.plugin_id = plugin_id
        self.method = method
        self.path = path
        self.handler_fn = handler_fn


class PluginLoader:
    """Discovers and loads plugins from server/plugins/*/."""

    def __init__(self, plugins_dir: Optional[Path] = None):
        self.plugins_dir = plugins_dir or PLUGINS_DIR
        self._manifests: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, PluginHandler] = {}  # key: "METHOD /path"
        self._modules: Dict[str, Any] = {}

    def discover(self) -> List[str]:
        """Scan plugins_dir for subdirectories containing manifest.json.
        Returns list of plugin IDs found."""
        discovered: List[str] = []
        if not self.plugins_dir.is_dir():
            return discovered
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / "manifest.json"
            if manifest_path.exists():
                discovered.append(entry.name)
        return discovered

    def load_plugin(self, plugin_id: str) -> bool:
        """Load a single plugin by ID. Returns True on success."""
        plugin_dir = self.plugins_dir / plugin_id
        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.exists():
            return False

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            import logging
            logging.warning("Failed to load plugin %s manifest: %s", plugin_id, exc)
            return False

        # Import the endpoints module
        # Add server/ to sys.path so "plugins.curate" can be imported
        server_str = str(SERVER_DIR)
        if server_str not in sys.path:
            sys.path.insert(0, server_str)

        try:
            module = importlib.import_module(f"plugins.{plugin_id}.endpoints")
        except ImportError as exc:
            import logging
            logging.warning("Failed to import plugin %s endpoints: %s", plugin_id, exc)
            return False

        # Register endpoints from manifest
        registered = 0
        for ep in manifest.get("endpoints", []):
            method = ep.get("method", "GET").upper()
            path = ep.get("path", "/")
            handler_name = ep.get("handler", "")

            if not handler_name:
                continue

            handler_fn = getattr(module, handler_name, None)
            if handler_fn is None:
                import logging
                logging.warning("Plugin %s: handler '%s' not found in endpoints module", plugin_id, handler_name)
                continue

            key = f"{method} {path}"
            self._handlers[key] = PluginHandler(plugin_id, method, path, handler_fn)
            registered += 1

        if registered > 0:
            self._manifests[plugin_id] = manifest
            self._modules[plugin_id] = module
            return True
        return False

    def load_all(self) -> int:
        """Discover and load all plugins. Returns count of successfully loaded plugins."""
        count = 0
        for plugin_id in self.discover():
            if self.load_plugin(plugin_id):
                count += 1
        return count

    def resolve(self, method: str, path: str) -> Optional[PluginHandler]:
        """Resolve a handler for the given HTTP method and path."""
        key = f"{method.upper()} {path}"
        return self._handlers.get(key)

    def get_manifest(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Get the manifest for a loaded plugin."""
        return self._manifests.get(plugin_id)

    def get_module(self, plugin_id: str) -> Any:
        """Get the endpoints module for a loaded plugin."""
        return self._modules.get(plugin_id)

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all loaded plugins with their manifests."""
        return [
            {"id": pid, **manifest}
            for pid, manifest in self._manifests.items()
        ]

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


# Singleton instance
_loader: Optional[PluginLoader] = None


def get_loader(plugins_dir: Optional[Path] = None) -> PluginLoader:
    """Get or create the singleton plugin loader."""
    global _loader
    if _loader is None:
        _loader = PluginLoader(plugins_dir)
        _loader.load_all()
    return _loader


def register_plugin(plugin_id: str, plugins_dir: Optional[Path] = None) -> bool:
    """Register a single plugin (convenience wrapper)."""
    loader = get_loader(plugins_dir)
    return loader.load_plugin(plugin_id)


def resolve_handler(method: str, path: str) -> Optional[PluginHandler]:
    """Resolve a handler for the given method + path."""
    loader = get_loader()
    return loader.resolve(method, path)


def dispatch_plugin_request(
    method: str,
    path: str,
    body: Dict[str, Any],
    params: Dict[str, List[str]],
    auth: Any = None,
) -> Tuple[bool, Dict[str, Any], int]:
    """Dispatch a request to a plugin handler.

    Returns:
      (handled, response_dict, status_code)
      handled=True means a plugin handled the request (response_dict is the body).
      handled=False means no plugin matched (caller should handle 404).
    """
    loader = get_loader()
    handler = loader.resolve(method, path)
    if handler is None:
        return False, {}, 404

    try:
        result = handler.handler_fn(body, params, auth)
        return True, result, 200
    except Exception as exc:
        # Check if it's a PluginError with status code
        status = getattr(exc, "status_code", 500)
        code = getattr(exc, "code", "internal_error")
        message = getattr(exc, "message", str(exc))
        return True, {"error": code, "detail": message}, status
