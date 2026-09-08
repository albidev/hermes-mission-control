/**
 * Plugin registry singleton — accessible from non-React modules
 * (hermes-api.ts, plugin endpoints.ts, etc.).
 *
 * The registry is created once at app boot in App.tsx and shared
 * everywhere via this module.
 */
import { PluginRegistry } from './plugins/registry';

let _registry: PluginRegistry | null = null;

/** Set the global plugin registry instance. Called once at app boot. */
export function setPluginRegistry(registry: PluginRegistry): void {
  _registry = registry;
}

/** Get the global plugin registry instance. */
export function getPluginRegistry(): PluginRegistry {
  if (!_registry) {
    _registry = new PluginRegistry();
  }
  return _registry;
}

/** Check if the registry has been initialized. */
export function hasPluginRegistry(): boolean {
  return _registry !== null;
}
