import type { MissionControlToolsetItem } from './hermes-api';

export const VISIBLE_TOOLS_PER_TOOLSET = 8;

export function toolsetMatches(item: MissionControlToolsetItem, query: string): boolean {
  const needle = query.trim().toLowerCase();
  return !needle || item.name.toLowerCase().includes(needle) ||
    item.resolvedTools.some((name) => name.toLowerCase().includes(needle));
}

/** Tools to show on a toolset card: matches for the query first, so a hit past the limit stays visible. */
export function visibleToolNames(item: MissionControlToolsetItem, query: string, limit = VISIBLE_TOOLS_PER_TOOLSET): string[] {
  const needle = query.trim().toLowerCase();
  const matching = needle ? item.resolvedTools.filter((name) => name.toLowerCase().includes(needle)) : [];
  const rest = item.resolvedTools.filter((name) => !matching.includes(name));
  return [...matching, ...rest].slice(0, limit);
}
