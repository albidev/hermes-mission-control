import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { translate, resolveLocale, LOCALE_STORAGE_KEY, type LocaleCode, type TranslateParams } from './i18n-core';

export type Locale = LocaleCode;
export { LOCALE_STORAGE_KEY };
export const SUPPORTED_LOCALES: readonly Locale[] = ['en', 'it'];

function readStoredLocale(): Locale {
  try {
    return resolveLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY));
  } catch {
    return 'en';
  }
}

function persistLocale(locale: Locale) {
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Ignore storage failures; browsers are allowed to be dramatic.
  }
}

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string, values?: TranslateParams) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const initialLocale = typeof window === 'undefined' ? 'en' : readStoredLocale();
  const [locale, setLocaleState] = useState<Locale>(initialLocale);

  const setLocale = useCallback((next: Locale) => {
    if (!SUPPORTED_LOCALES.includes(next)) return;
    setLocaleState(next);
    persistLocale(next);
  }, []);

  useEffect(() => {
    if (typeof document === 'undefined') return;
    document.documentElement.lang = locale;
  }, [locale]);

  const t = useCallback((key: string, values?: TranslateParams) => translate(key, values, locale), [locale]);

  const value = useMemo<I18nContextValue>(() => ({ locale, setLocale, t }), [locale, setLocale, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error('useI18n must be used inside I18nProvider');
  }
  return context;
}
