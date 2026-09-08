#!/bin/bash
# Setup script — creates symlinks from src/plugins/ to installed external plugins.
# Run this after cloning a plugin into ~/.hermes/mc-plugins/<id>/
#
# Usage: bash scripts/setup-plugins.sh

set -euo pipefail

MC_PLUGINS_DIR="${HOME}/.hermes/mc-plugins"
SRC_PLUGINS_DIR="$(cd "$(dirname "$0")/../src/plugins" && pwd)"

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
                continue
            fi
        fi
        
        echo "  Linking ${plugin_id} -> ${ui_dir}"
        ln -sfn "$ui_dir" "$link_path"
        created=$((created + 1))
    done
fi

echo
echo "Done. ${created} plugin(s) linked."
echo "Restart Vite to pick up changes."
