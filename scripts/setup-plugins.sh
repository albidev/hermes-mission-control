#!/bin/bash
# Setup script — creates symlinks from src/plugins/ to installed external plugins.
# Run this after cloning a plugin into ~/.hermes/mc-plugins/<id>/
#
# Usage: bash scripts/setup-plugins.sh

set -euo pipefail

MC_PLUGINS_DIR="${HOME}/.hermes/mc-plugins"
SRC_PLUGINS_ROOT="$(cd "$(dirname "$0")/../src" && pwd)"
SRC_PLUGINS_DIR="${SRC_PLUGINS_ROOT}/plugins"
mkdir -p "$SRC_PLUGINS_DIR"

echo "=== MC Plugin Setup ==="
echo "External plugins: ${MC_PLUGINS_DIR}"
echo "Source plugins:   ${SRC_PLUGINS_DIR}"
echo

# Remove stale symlinks (pointing to non-existent targets)
if [ -d "$SRC_PLUGINS_DIR" ]; then
    for link in "$SRC_PLUGINS_DIR"/*/; do
        [ -L "$link" ] || continue
        target=$(readlink "$link")
        if [ ! -d "$target" ]; then
            echo "  Removing stale symlink: $(basename "$link")"
            rm "$link"
        fi
    done
fi

# Create symlinks for installed plugins
created=0
if [ -d "$MC_PLUGINS_DIR" ]; then
    for plugin_dir in "$MC_PLUGINS_DIR"/*/; do
        [ -d "$plugin_dir" ] || continue
        plugin_id=$(basename "$plugin_dir")
        ui_dir="$plugin_dir/ui"
        
        if [ ! -d "$ui_dir" ]; then
            echo "  Skipping ${plugin_id}: no ui/ directory"
            continue
        fi
        
        link_path="$SRC_PLUGINS_DIR/$plugin_id"

        if [ -L "$link_path" ]; then
            existing=$(readlink "$link_path")
            if [ "$existing" = "$ui_dir" ] || [ "$existing" = "$ui_dir/" ]; then
                echo "  ${plugin_id}: already linked"
            fi
        fi

        if [ ! -L "$link_path" ] || [ "$(readlink "$link_path")" != "$ui_dir" -a "$(readlink "$link_path")" != "$ui_dir/" ]; then
            echo "  Linking ${plugin_id} -> ${ui_dir}"
            ln -sfn "$ui_dir" "$link_path"
            created=$((created + 1))
        fi

        # External plugin sources live outside the repo tree at their real
        # path (~/.hermes/mc-plugins/<id>/), not through the src/plugins/
        # symlink target. Vite's transform pipeline (and PostCSS's CSS
        # resolver) walk up from that REAL path looking for node_modules, so
        # a plugin's own bare imports (CodeMirror, tailwind subpaths, etc.)
        # 404 unless the host's node_modules is reachable from there. A
        # node_modules symlink at the plugin root covers every file under
        # ui/ via Node's normal upward walk, without requiring per-package
        # aliases in vite.config.ts for every dependency the plugin ships.
        plugin_node_modules="${plugin_dir}node_modules"
        host_node_modules="${SRC_PLUGINS_ROOT}/../node_modules"
        if [ -L "$plugin_node_modules" ] || [ ! -e "$plugin_node_modules" ]; then
            ln -sfn "$host_node_modules" "$plugin_node_modules"
            echo "  ${plugin_id}: linked node_modules -> host"
        else
            echo "  ${plugin_id}: node_modules exists and is not a symlink, skipping"
        fi
    done
fi

echo
echo "Done. ${created} plugin(s) linked."
echo "Restart Vite to pick up changes."
