/**
 * Integration tests for the i18n feature set:
 *   1. Catalog loading for en and it
 *   2. Locale-aware formatting outputs
 *   3. Language switching behavior
 *   4. Fallback when keys are missing
 *   5. Persistence of locale preference
 *
 * Pure logic (translation, persistence resolution, formatting) is exercised
 * directly under node --experimental-strip-types. Source-level assertions check
 * that the runtime layer (provider + switcher) wires that logic together.
 */
import { readFileSync } from 'node:fs';
import {
  translate,
  createTranslator,
  resolveMessage,
  interpolate,
  resolveLocale,
  LOCALE_STORAGE_KEY,
  CATALOGS,
  FALLBACK_LOCALE,
} from '../src/lib/i18n-core.ts';
import {
  formatDate,
  formatTime,
  formatDateTime,
  formatNumber,
  formatCurrency,
} from '../src/lib/format.ts';

function assert(condition: unknown, label: string): void {
  if (!condition) throw new Error(`FAIL: ${label}`);
  console.log(`ok - ${label}`);
}

const en = CATALOGS.en;
const it = CATALOGS.it;

// =========================================================================
// 1. Catalog loading for en and it
// =========================================================================
assert(CATALOGS.en && Object.keys(CATALOGS.en).length > 0, 'en catalog loads and is non-empty');
assert(CATALOGS.it && Object.keys(CATALOGS.it).length > 0, 'it catalog loads and is non-empty');
assert(typeof en['nav.overview'] === 'string' && en['nav.overview'].length > 0, 'en nav.overview is a non-empty string');
assert(typeof it['nav.overview'] === 'string' && it['nav.overview'].length > 0, 'it nav.overview is a non-empty string');
assert(
  Object.keys(en).length === Object.keys(it).length,
  `en and it catalogs have equal key counts (${Object.keys(en).length} each)`,
);
const missingInIt = Object.keys(en).filter((k) => !(k in it));
assert(missingInIt.length === 0, 'every en key exists in it');
assert(it['auth.unlock'] !== en['auth.unlock'], 'it and en differ for at least one key (translation is real)');

// =========================================================================
// 2. Locale-aware formatting outputs
// =========================================================================
const ts = Date.UTC(2026, 7, 26, 14, 5); // 2026-08-26T14:05Z
// The two locales render the same instant; assert representative output for each.
assert(formatDate(ts, 'en-GB').includes('2026'), 'en-GB date includes the year');
assert(formatDate(ts, 'it-IT').includes('2026'), 'it-IT date includes the year');
assert(formatDate(ts, 'it-IT').toLowerCase().includes('ago'), 'it-IT date renders an Italian month name');
assert(!formatDate(ts, 'en-GB').toLowerCase().includes('ago'), 'en-GB date does not use the Italian month name');

// Number formatting: it-IT uses the comma decimal separator, en-US the period.
// Use a sub-thousand value so grouping separators don't muddy the assertion.
assert(formatNumber(0.5, 'it-IT') === '0,5', 'it-IT number uses comma decimal separator');
assert(formatNumber(0.5, 'en-US') === '0.5', 'en-US number uses period decimal separator');

// Currency is locale/currency-aware.
assert(formatCurrency(12.5, 'EUR', 'it-IT').includes('€'), 'it-IT EUR formats with the euro sign');
assert(formatCurrency(12.5, 'USD', 'en-US').includes('$'), 'en-US USD formats with the dollar sign');

// Date/time outputs carry the expected components.
assert(formatTime(ts, 'en-GB').includes(':'), 'formatTime renders an hour:minute');
assert(formatDateTime(ts, 'en-GB').includes('2026'), 'formatDateTime includes the year');

// Invalid inputs degrade gracefully rather than throwing.
assert(formatDate(null) === 'n/a', 'formatDate(null) is n/a');
assert(formatNumber(NaN) !== '' , 'formatNumber(NaN) returns without throwing');

// =========================================================================
// 3. Language switching behavior
// =========================================================================
// The bound translator is the switching primitive: a component swaps its
// locale by building a new translator, so same key, different locale => different string.
const tEn = createTranslator('en');
const tIt = createTranslator('it');
assert(tEn('nav.overview') === en['nav.overview'], 'en translator resolves nav.overview');
assert(tIt('nav.overview') === it['nav.overview'], 'it translator resolves nav.overview');
assert(tIt('nav.overview') !== tEn('nav.overview'), 'switching locale changes the resolved string');

// translate() honours an explicit locale argument.
assert(translate('nav.overview', undefined, 'it') === it['nav.overview'], 'translate(it) returns the Italian string');
assert(translate('nav.overview', undefined, 'en') === en['nav.overview'], 'translate(en) returns the English string');

// =========================================================================
// 4. Fallback when keys are missing
// =========================================================================
const stubEn: Record<string, string> = { 'a.present': 'English A', 'only.en': 'Only English' };
const stubIt: Record<string, string> = { 'a.present': 'Italiano A' };
assert(
  resolveMessage('only.en', stubIt, stubEn) === 'Only English',
  'key missing in selected locale falls back to the fallback catalog',
);
assert(
  resolveMessage('a.present', stubIt, stubEn) === 'Italiano A',
  'key present in selected locale wins over the fallback',
);
assert(
  resolveMessage('nope.missing', stubIt, stubEn) === 'nope.missing',
  'key missing in both locales resolves to the key name',
);
assert(
  translate('nav.overview', undefined, 'xx' as never) === en['nav.overview'],
  'unsupported locale falls back to English',
);

// Interpolation on fallback resolution.
assert(
  resolveMessage('only.en', stubIt, stubEn) === 'Only English',
  'fallback value used when present in fallback catalog',
);
assert(
  translate('overview.queued', { count: 7 }, 'en') === en['overview.queued'].replace('{count}', '7'),
  'translate interpolates placeholders',
);
assert(
  interpolate('Hello {name}', { name: undefined }) === 'Hello {name}',
  'undefined param leaves the placeholder token intact',
);

// =========================================================================
// 5. Persistence of locale preference
// =========================================================================
assert(LOCALE_STORAGE_KEY === 'mission-control-locale', 'persistence key is mission-control-locale');
assert(resolveLocale('it') === 'it', 'stored "it" resolves to it');
assert(resolveLocale('en') === 'en', 'stored "en" resolves to en');
assert(resolveLocale(null) === 'en', 'missing stored value defaults to en');
assert(resolveLocale(undefined) === 'en', 'undefined stored value defaults to en');
assert(resolveLocale('xx') === 'en', 'unknown stored value defaults to en');
assert(resolveLocale('') === 'en', 'empty stored value defaults to en');
assert(FALLBACK_LOCALE === 'en', 'fallback locale is en');

// =========================================================================
// 6. Runtime wiring: provider + switcher use the shared core
// =========================================================================
const provider = readFileSync(new URL('../src/lib/i18n.tsx', import.meta.url), 'utf8');
assert(provider.includes('LOCALE_STORAGE_KEY'), 'provider reads/writes the persisted key');
assert(provider.includes('localStorage.getItem'), 'provider restores the locale from storage');
assert(provider.includes('localStorage.setItem'), 'provider persists the locale to storage');
assert(provider.includes('document.documentElement.lang'), 'provider syncs the html lang attribute');
assert(provider.includes('resolveLocale'), 'provider delegates stored-locale normalization to resolveLocale');
assert(provider.includes("from './i18n-core'"), 'provider imports the shared resolution core');

const switcher = readFileSync(new URL('../src/components/LanguageSwitcher.tsx', import.meta.url), 'utf8');
assert(switcher.includes('useI18n'), 'switcher consumes the i18n context');
assert(switcher.includes('setLocale'), 'switcher calls setLocale to switch language');
assert(switcher.includes("setLocale('en')") && switcher.includes("setLocale('it')"), 'switcher offers both en and it');
assert(switcher.includes('aria-pressed'), 'switcher exposes the active locale to assistive tech');

console.log('i18n integration tests passed.');
