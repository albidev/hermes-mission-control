import { readFileSync } from 'node:fs';

function assert(condition: boolean, message: string) {
  if (!condition) {
    throw new Error(message);
  }
}

const enRaw = readFileSync(new URL('../src/locales/en.json', import.meta.url), 'utf8');
const itRaw = readFileSync(new URL('../src/locales/it.json', import.meta.url), 'utf8');
const en: Record<string, string> = JSON.parse(enRaw);
const it: Record<string, string> = JSON.parse(itRaw);

// 1) Catalog structure and key completeness
const enKeys = Object.keys(en).sort();
const itKeys = Object.keys(it).sort();
assert(enKeys.length > 600, `expected a large English catalog, got ${enKeys.length}`);
assert(
  JSON.stringify(enKeys) === JSON.stringify(itKeys),
  `English and Italian catalogs must have identical key sets. Missing in it: ${enKeys.filter((k) => !(k in it))}. Missing in en: ${itKeys.filter((k) => !(k in en))}.`,
);
for (const key of enKeys) {
  assert(typeof en[key] === 'string' && en[key].length > 0, `en.${key} is empty`);
  assert(typeof it[key] === 'string' && it[key].length > 0, `it.${key} is empty`);
}

// 2. Switcher strings are present in both catalogs. Language names are
//    self-referential by convention (English / Italiano shown as-is in both).
for (const key of ['lang.label', 'lang.english', 'lang.italian', 'lang.englishTitle', 'lang.italianTitle']) {
  assert(key in en, `en is missing ${key}`);
  assert(key in it, `it is missing ${key}`);
}
assert(en['lang.english'] === 'English', 'en shows English in its own language');
assert(en['lang.italian'] === 'Italiano', 'en shows Italiano in its own language');
assert(it['lang.english'] === 'English', 'it shows English in its own language');
assert(it['lang.italian'] === 'Italiano', 'it shows Italiano in its own language');
assert(en['lang.englishTitle'] === 'Switch to English', 'en English title');
assert(it['lang.italianTitle'] === "Passa all'italiano", 'it Italian title');

// 3. Localized shell strings actually differ between locales
assert(it['nav.overview'] === 'Panoramica', 'it nav.overview is translated');
assert(it['nav.overview'] !== en['nav.overview'], 'Italian differs from English for nav labels');
assert(it['auth.unlock'] !== en['auth.unlock'], 'auth action differs between locales');

// 4. Provider wires locale state, persistence, and the document attribute
const provider = readFileSync(new URL('../src/lib/i18n.tsx', import.meta.url), 'utf8');
assert(provider.includes('LOCALE_STORAGE_KEY'), 'locale persistence key defined');
assert(provider.includes('localStorage.setItem'), 'locale is written to storage');
assert(provider.includes('localStorage.getItem'), 'locale is restored from storage');
assert(provider.includes('document.documentElement.lang'), 'locale updates the document lang attribute');
assert(provider.includes('setLocaleState'), 'provider holds locale in React state');
assert(provider.includes("from './i18n-core'"), 'provider reuses the shared resolution core');

// 5. Language switcher UI wiring
const switcher = readFileSync(new URL('../src/components/LanguageSwitcher.tsx', import.meta.url), 'utf8');
assert(switcher.includes('useI18n'), 'switcher consumes the i18n context');
assert(switcher.includes('setLocale'), 'switcher calls setLocale on selection');
assert(switcher.includes("setLocale('en')"), 'switcher offers English');
assert(switcher.includes("setLocale('it')"), 'switcher offers Italian');
assert(switcher.includes('aria-pressed'), 'switcher exposes active locale to assistive tech');

// 6. Switcher is mounted in the shell and chrome strings are localized
const shell = readFileSync(new URL('../src/components/MissionControlShell.tsx', import.meta.url), 'utf8');
assert(shell.includes('LanguageSwitcher'), 'language switcher is mounted in the shell');
assert(shell.includes('useI18n'), 'shell consumes i18n for localized strings');
assert(shell.includes("t('nav.overview')"), 'shell nav labels route through t()');
assert(shell.includes("t('auth.unlock')"), 'shell auth strings route through t()');

console.log('i18n switcher, persistence, and catalog tests passed');
