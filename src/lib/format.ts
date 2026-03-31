export function formatTimestamp(value: unknown) {
  if (value === null || value === undefined || value === '') {
    return 'n/a';
  }

  if (typeof value === 'number') {
    const ms = value > 1e12 ? value : value * 1000;
    return new Date(ms).toLocaleString();
  }

  if (typeof value === 'string') {
    const numeric = Number(value);
    if (!Number.isNaN(numeric) && value.trim() !== '') {
      const ms = numeric > 1e12 ? numeric : numeric * 1000;
      return new Date(ms).toLocaleString();
    }

    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toLocaleString();
    }
  }

  return String(value);
}

export function shortTimestamp(value: unknown) {
  if (value === null || value === undefined || value === '') return 'n/a';
  return formatTimestamp(value);
}

export function formatRelativeTime(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'n/a';

  let ms: number;

  if (typeof value === 'number') {
    ms = value > 1e12 ? value : value * 1000;
  } else if (typeof value === 'string') {
    const numeric = Number(value);
    ms = !Number.isNaN(numeric) && value.trim() !== ''
      ? (numeric > 1e12 ? numeric : numeric * 1000)
      : new Date(value).getTime();
  } else {
    return 'n/a';
  }

  const diff = Date.now() - ms;
  const seconds = Math.floor(diff / 1000);

  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
