#!/usr/bin/env python3
"""Plugin loader — discovers plugins, loads manifests, dispatches requests.

Architecture:
  1. Each plugin has a manifest.json describing its endpoints
  2. Each plugin has an endpoints.py exporting handler functions
  3. The loader discovers all plugins at startup and registers them
  4. The telemetry server delegates plugin requests to the loader

Plugin sources (searched in order):
  - Internal: server/plugins/*/  (bundled with MC)
  - External: ~/.hermes/mc-plugins/*/  (installed via git clone)

The loader is a singleton — call get_loader() from anywhere.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


SERVER_DIR = Path(__file__).resolve().parent.parent
INTERNAL_PLUGINS_DIR = Path(__file__).resolve().parent
EXTERNAL_PLUGINS_DIR = Path(os.path.expanduser("~/.hermes/mc-plugins"))


class PluginHandler:
    """Resolved handler with metadata."""

    def __init__(self, plugin_id: str, method: str, path: str, handler_fn: Callable):
        self.plugin_id = plugin_id
        self.method = method
        self.path = path
        self.handler_fn = handler_fn


class PluginLoader:
    """Discovers and loads plugins from internal and external directories."""

    def __init__(
        self,
        internal_dir: Optional[Path] = None,
        external_dir: Optional[Path] = None,
    ):
        self.internal_dir = internal_dir or INTERNAL_PLUGINS_DIR
        self.external_dir = external_dir or EXTERNAL_PLUGINS_DIR
        self._manifests: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, PluginHandler] = {}  # key: "METHOD /path"
        self._modules: Dict[str, Any] = {}
        self._plugin_dirs: Dict[str, Path] = {}  # plugin_id -> resolved dir

    def discover(self) -> List[str]:
        """Scan internal and external dirs for subdirectories containing manifest.json.
        Returns list of plugin IDs found. External plugins override internal with same ID."""
        discovered: Dict[str, Path] = {}

        # Internal first
        if self.internal_dir.is_dir():
            for entry in sorted(self.internal_dir.iterdir()):
                if not entry.is_dir():
                    continue
                manifest_path = entry / "manifest.json"
                if manifest_path.exists():
                    discovered[entry.name] = entry

        # External overrides (same ID = override)
        if self.external_dir.is_dir():
            for entry in sorted(self.external_dir.iterdir()):
                if not entry.is_dir():
                    continue
                manifest_path = entry / "manifest.json"
                if manifest_path.exists():
                    discovered[entry.name] = entry

        return list(discovered.keys())

    def _resolve_plugin_dir(self, plugin_id: str) -> Optional[Path]:
        """Resolve the directory for a plugin, preferring external over internal."""
        # Check external first (overrides)
        ext_dir = self.external_dir / plugin_id
        if (ext_dir / "manifest.json").exists():
            return ext_dir
        int_dir = self.internal_dir / plugin_id
        if (int_dir / "manifest.json").exists():
            return int_dir
        return None

    def load_plugin(self, plugin_id: str) -> bool:
        """Load a single plugin by ID. Returns True on success.
        
        For internal plugins (in server/plugins/), imports as plugins.<id>.endpoints.
        For external plugins (in ~/.hermes/mc-plugins/), loads directly from path.
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        if plugin_dir is None:
            return False

        manifest_path = plugin_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            import logging
            logging.warning("Failed to load plugin %s manifest: %s", plugin_id, exc)
            return False

        # Determine if this is an external plugin
        is_external = str(plugin_dir).startswith(str(self.external_dir))

        # Import the endpoints module
        if is_external:
            module = self._load_external_module(plugin_dir, plugin_id)
        else:
            module = self._load_internal_module(plugin_id)

        if module is None:
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
            self._plugin_dirs[plugin_id] = plugin_dir
            return True
        return False

    def _load_internal_module(self, plugin_id: str):
        """Load an internal plugin module (from MC's server/plugins/ dir)."""
        server_str = str(SERVER_DIR)
        if server_str not in sys.path:
            sys.path.insert(0, server_str)
        try:
            return importlib.import_module(f"plugins.{plugin_id}.endpoints")
        except ImportError as exc:
            import logging
            logging.warning("Failed to import internal plugin %s: %s", plugin_id, exc)
            return None

    def _load_external_module(self, plugin_dir: Path, plugin_id: str):
        """Load an external plugin module from an arbitrary path."""
        endpoints_path = plugin_dir / "endpoints.py"
        if not endpoints_path.exists():
            import logging
            logging.warning("External plugin %s: endpoints.py not found at %s", plugin_id, endpoints_path)
            return None

        # Ensure the plugin dir is on sys.path so relative imports work
        plugin_str = str(plugin_dir)
        if plugin_str not in sys.path:
            sys.path.insert(0, plugin_str)

        # Also ensure MC's server/ is on sys.path (for hermes_paths etc.)
        server_str = str(SERVER_DIR)
        if server_str not in sys.path:
            sys.path.insert(0, server_str)

        try:
            spec = importlib.util.spec_from_file_location(
                f"mc_plugin_{plugin_id}",
                endpoints_path,
                submodule_search_locations=[str(plugin_dir)],
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"mc_plugin_{plugin_id}"] = module
            spec.loader.exec_module(module)
            return module
        except Exception as exc:
            import logging
            logging.warning("Failed to load external plugin %s from %s: %s", plugin_id, plugin_dir, exc)
            return None

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

    def get_plugin_dir(self, plugin_id: str) -> Optional[Path]:
        """Get the resolved directory for a loaded plugin."""
        return self._plugin_dirs.get(plugin_id)

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all loaded plugins with their manifests."""
        return [
            {"id": pid, **manifest, "dir": str(self._plugin_dirs.get(pid, ""))}
            for pid, manifest in self._manifests.items()
        ]

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


# Singleton instance
_loader: Optional[PluginLoader] = None


def get_loader(
    internal_dir: Optional[Path] = None,
    external_dir: Optional[Path] = None,
) -> PluginLoader:
    """Get or create the singleton plugin loader."""
    global _loader
    if _loader is None:
        _loader = PluginLoader(internal_dir, external_dir)
        _loader.load_all()
    return _loader


def register_plugin(plugin_id: str, plugins_dir: Optional[Path] = None) -> bool:
    """Register a single plugin (convenience wrapper)."""
    loader = get_loader()
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
    # HTTP handlers declare paths relative to /api/local, while the
    # BaseHTTPRequestHandler receives the full local API path.
    plugin_path = path
    if plugin_path.startswith('/api/local'):
        plugin_path = plugin_path[len('/api/local'):] or '/'
    handler = loader.resolve(method, plugin_path)
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
