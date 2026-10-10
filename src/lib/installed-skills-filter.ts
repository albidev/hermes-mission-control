export type InstalledSkillStatus = 'all' | 'enabled' | 'disabled';
type SearchableSkill = { name: string; description: string; tags: string[]; enabled: boolean };
export function filterInstalledSkills<T extends SearchableSkill>(items: readonly T[], query: string, status: InstalledSkillStatus): T[] {
  const needle = query.trim().toLowerCase();
  return items.filter(item =>
    (status === 'all' || item.enabled === (status === 'enabled')) &&
    (!needle || [item.name, item.description, ...item.tags].join(' ').toLowerCase().includes(needle)),
  );
}
