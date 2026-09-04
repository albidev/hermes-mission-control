import React from 'react';
import * as Lucide from 'lucide-react';

export type IconMap = Record<string, React.ComponentType<any>>;

/** Resolve an icon by name (string from plugin manifest) to a Lucide component */
export function resolveIcon(name: string): React.ComponentType<any> | undefined {
  return (Lucide as unknown as IconMap)[name];
}
