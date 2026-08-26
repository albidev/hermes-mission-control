/**
 * Contract tests for the i18n fallback / missing-key resolution logic.
 * Executed via node --experimental-strip-types (see package.json test:ui pattern).
 */
import {
  resolveMessage,
  createTranslator,
  interpolate,
  translate,
  FALLBACK_LOCALE,
  CATALOGS,
} from '../src/lib/i18n-core.ts';

function assert(condition: unknown, label: string): void {
  if (!condition) throw new Error(`FAIL: ${label}`);
  console.log(`ok - ${label}`);
}

const en = CATALOGS.en;
const it = CATALOGS.it;

// --- 1. Present keys resolve in both locales ---
assert(translate('nav.overview', undefined, 'en') === en['nav.overview'], 'en resolves a present key');
assert(translate('nav.overview', undefined, 'it') === it['nav.overview'], 'it resolves a present key');

// --- 2. English is the fallback for a key missing only in the selected locale ---
// Use pure resolveMessage with stub catalogs so the fallback path is exercised
// deterministically (real catalogs are in full parity).
const stubEn: Record<string, string> = { 'a.present': 'English A', 'only.en': 'Only English' };
const stubIt: Record<string, string> = { 'a.present': 'Italiano A' };
assert(
  resolveMessage('only.en', stubIt, stubEn) === 'Only English',
  'key missing in selected locale falls back to the fallback (English) catalog',
);
assert(
  resolveMessage('a.present', stubIt, stubEn) === 'Italiano A',
  'present key in selected locale is used over fallback',
);

// --- 3. Double-missing keys resolve to the key name itself ---
assert(
  resolveMessage('nope.missing', stubIt, stubEn) === 'nope.missing',
  'double-missing shows the key name',
);

// --- 4. Unknown locale falls back to the English catalog ---
assert(
  translate('nav.overview', undefined, 'xx' as never) === en['nav.overview'],
  'unsupported locale falls back to English',
);

// --- 5. Interpolation ---
assert(
  translate('overview.queued', { count: 3 }, 'en') === en['overview.queued'].replace('{count}', '3'),
  'en interpolates params',
);
assert(
  translate('overview.queued', { count: 5 }, 'it') === it['overview.queued'].replace('{count}', '5'),
  'it interpolates params',
);
assert(interpolate('Hello {name}', {}) === 'Hello {name}', 'missing param leaves the literal token');
assert(interpolate('Hello {name}', { name: 'Albi' }) === 'Hello Albi', 'interpolate substitutes a param');
assert(interpolate('Hello {name}', { name: undefined }) === 'Hello {name}', 'undefined param leaves the literal token');

// --- 6. Bound translator ---
const tIt = createTranslator('it');
assert(tIt('nav.overview', undefined) === it['nav.overview'], 'bound translator targets it');
const tEn = createTranslator('en');
assert(tEn('nav.overview', undefined) === en['nav.overview'], 'bound translator targets en');

// --- 7. Catalogs are well-formed and have a fallback target ---
assert(Object.keys(en).length > 0 && Object.keys(it).length > 0, 'catalogs are non-empty');
assert(FALLBACK_LOCALE === 'en', 'FALLBACK_LOCALE is en');
assert(en['nav.overview'] !== undefined && it['nav.overview'] !== undefined, 'shared key present in both');

console.log('i18n fallback tests passed.');
