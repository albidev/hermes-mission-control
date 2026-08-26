import enRaw from '../locales/en.json' with { type: 'json' };
import itRaw from '../locales/it.json' with { type: 'json' };

/**
 * i18n resolution core — locale fallback + missing-key handling.
 *
 * The React provider / language-switcher card (t_0dae74fc) wires this into a
 * Context; the formatter card (t_8f24919b) handles Intl number/date/currency.
 * This module owns ONLY key resolution: selected-locale → English fallback →
 * key-name placeholder, plus dev-mode missing-key logging.
 */

export type LocaleCode = 'en' | 'it';
export type MessageCatalog = Record<string, string>;
export type TranslateParams = Record<string, string | number | boolean | null | undefined>;

// JSON imports are inferred as exact literal types; re-cast so arbitrary
// (including missing) keys can be looked up safely.
export const CATALOGS: Record<LocaleCode, MessageCatalog> = {
  en: enRaw as MessageCatalog,
  it: itRaw as MessageCatalog,
};

export const FALLBACK_LOCALE: LocaleCode = 'en';

/** Development-mode detection, guarded for non-Vite runners. */
function isDevMode(): boolean {
  try {
    return typeof import.meta !== 'undefined' && import.meta.env?.DEV === true;
  } catch {
    return false;
  }
}

/** Substitute `{name}` placeholders; a missing/absent param leaves the literal token. */
export function interpolate(template: string, params?: TranslateParams): string {
  if (!params || Object.keys(params).length === 0) return template;
  return template.replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (_m, name: string) => {
    const value = params[name];
    return value === undefined || value === null ? `{${name}}` : String(value);
  });
}

/**
 * Pure resolver over two catalogs. Returns:
 *  - selected[key] if present,
 *  - else fallback[key],
 *  - else the key name itself.
 * `logMissing` gates the dev-mode warning so the same primitive serves both the
 * live `translate` (logs in dev) and unit tests (can disable).
 */
export function resolveMessage(
  key: string,
  selected: MessageCatalog,
  fallback: MessageCatalog,
  params?: TranslateParams,
  logMissing: boolean = false,
): string {
  let value: string | undefined = selected[key];

  if (value === undefined && fallback !== selected) {
    value = fallback[key];
    if (value !== undefined && logMissing) {
      console.warn(`[i18n] missing key '${key}' in selected locale, fell back to '${FALLBACK_LOCALE}'`);
    }
  }

  if (value === undefined) {
    if (logMissing) {
      console.warn(`[i18n] missing translation key in both locales: ${key}`);
    }
    return key;
  }

  return interpolate(value, params);
}

/**
 * Resolve a key against the requested locale with the required fallback chain:
 *   selected locale → English → key name.
 * Missing keys are logged to the console in development mode.
 */
export function translate(
  key: string,
  params?: TranslateParams,
  locale: LocaleCode = FALLBACK_LOCALE,
): string {
  const catalog: MessageCatalog = CATALOGS[locale] ?? CATALOGS[FALLBACK_LOCALE];
  return resolveMessage(key, catalog, CATALOGS[FALLBACK_LOCALE], params, isDevMode());
}

/** Bound translator for a fixed locale (convenience for single-language components). */
export function createTranslator(locale: LocaleCode) {
  return (key: string, params?: TranslateParams) => translate(key, params, locale);
}
