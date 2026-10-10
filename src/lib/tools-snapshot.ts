import type { MissionControlToolCatalogItem, MissionControlToolsSnapshot, MissionControlToolsetItem } from './hermes-api';

// Maps the core dashboard's GET /api/tools/toolsets rows
// (hermes_cli/web_routers/tools.py) onto the Tools route snapshot. Core owns the
// semantics: `enabled` is the toolset switch on its configuration platform and
// `configured` is credential presence resolved in the profile's secret scope.

type CoreToolsetRow = {
  name?: unknown;
  label?: unknown;
  description?: unknown;
  enabled?: unknown;
  available?: unknown;
  configured?: unknown;
  tools?: unknown;
};

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function toolNames(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(text).filter(Boolean))];
}

function toToolset(row: CoreToolsetRow): MissionControlToolsetItem | null {
  const name = text(row.name);
  if (!name) return null;
  const tools = toolNames(row.tools);
  const enabled = typeof row.enabled === 'boolean' ? row.enabled : row.available !== false;
  return {
    name,
    description: text(row.label) || text(row.description),
    directTools: tools,
    includes: [],
    resolvedTools: tools,
    toolCount: tools.length,
    isComposite: false,
    available: enabled,
    configured: row.configured !== false,
  };
}

export function toolsSnapshotFromToolsets(payload: unknown): MissionControlToolsSnapshot {
  if (!Array.isArray(payload)) throw new TypeError('Tools endpoint did not return a toolset list.');
  const toolsets = payload
    .filter((row): row is CoreToolsetRow => Boolean(row) && typeof row === 'object' && !Array.isArray(row))
    .map(toToolset)
    .filter((item): item is MissionControlToolsetItem => item !== null);

  const catalog = new Map<string, MissionControlToolCatalogItem>();
  for (const toolset of toolsets) {
    for (const name of toolset.resolvedTools) {
      if (!catalog.has(name)) catalog.set(name, { name, toolset: toolset.name, available: toolset.available });
    }
  }
  const toolCatalog = [...catalog.values()];

  return {
    available: true,
    count: toolsets.length,
    toolCount: toolCatalog.length,
    toolsets,
    availableToolsets: toolsets,
    toolCatalog,
    resolvedTools: toolCatalog.map((item) => item.name),
  };
}
