import type { MissionControlToolsetItem } from './hermes-api';

export function toolsetMatches(item: MissionControlToolsetItem, query: string): boolean {
  const needle = query.trim().toLowerCase();
  return !needle || item.name.toLowerCase().includes(needle) ||
    item.resolvedTools.some((name) => name.toLowerCase().includes(needle));
}
