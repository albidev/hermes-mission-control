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

/**
 * Locale-aware formatting primitives built on the Intl API.
 *
 * Pure functions (no React/DOM dependency) so they run under plain Node and
 * are directly unit-testable. A small `Intl` guard lets them be imported in
 * runtimes where full ICU data is unavailable (e.g. a stripped-down Node
 * binary) without crashing — they fall back to the raw value in that case.
 */

function hasIntl(): boolean {
  return typeof Intl !== 'undefined' && typeof Intl.DateTimeFormat === 'function';
}

export function formatDate(value: unknown, locale: string = 'en'): string {
  if (value === null || value === undefined || value === '') return 'n/a';
  const date = value instanceof Date ? value : new Date(value as number | string);
  if (Number.isNaN(date.getTime())) return String(value);
  if (!hasIntl()) return date.toISOString().slice(0, 10);
  return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'short', day: 'numeric' }).format(date);
}

export function formatTime(value: unknown, locale: string = 'en'): string {
  if (value === null || value === undefined || value === '') return 'n/a';
  const date = value instanceof Date ? value : new Date(value as number | string);
  if (Number.isNaN(date.getTime())) return String(value);
  if (!hasIntl()) return date.toISOString().slice(11, 16);
  return new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit' }).format(date);
}

export function formatDateTime(value: unknown, locale: string = 'en'): string {
  if (value === null || value === undefined || value === '') return 'n/a';
  const date = value instanceof Date ? value : new Date(value as number | string);
  if (Number.isNaN(date.getTime())) return String(value);
  if (!hasIntl()) return date.toISOString();
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}

export function formatNumber(value: number, locale: string = 'en'): string {
  if (!hasIntl()) return String(value);
  return new Intl.NumberFormat(locale).format(value);
}

export function formatCurrency(value: number, currency: string, locale: string = 'en'): string {
  if (!hasIntl()) return `${currency} ${value}`;
  return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(value);
}

export function formatPercent(value: number, locale: string = 'en'): string {
  if (!hasIntl()) return `${value}%`;
  return new Intl.NumberFormat(locale, { style: 'percent' }).format(value);
}

