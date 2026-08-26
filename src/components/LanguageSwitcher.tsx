import { useI18n } from '../lib/i18n';

function BritishFlag() {
  return (
    <svg viewBox="0 0 24 24" className="lang-icon" aria-hidden>
      <rect x="3" y="5" width="18" height="14" rx="1.5" fill="#fff" stroke="currentColor" strokeWidth="0.8" />
      <path d="M3 5l7.5 5.2L18 5M3 19l7.5-5.2L18 19M6 5v14M18 5v14M3 9.6h18M3 14.4h18" stroke="#012169" strokeWidth="0.9" fill="none" strokeLinejoin="round" />
    </svg>
  );
}

function ItalianFlag() {
  return (
    <svg viewBox="0 0 24 24" className="lang-icon" aria-hidden>
      <rect x="2" y="5" width="18" height="14" rx="1.5" fill="#fff" stroke="currentColor" strokeWidth="0.8" />
      <rect x="2" y="5" width="6" height="14" rx="1" fill="#009246" />
      <rect x="16" y="5" width="6" height="14" rx="1" fill="#ce2b37" />
    </svg>
  );
}

export function LanguageSwitcher({ showLabel = true, className = '' }: { showLabel?: boolean; className?: string }) {
  const { locale, setLocale, t } = useI18n();

  return (
    <div className={`lang-toggle ${className}`.trim()} role="group" aria-label={t('lang.label')}>
      {showLabel ? <span className="lang-toggle-label">{t('lang.label')}</span> : null}
      <div className="lang-toggle-track">
        <button
          type="button"
          title={t('lang.englishTitle')}
          className={`lang-toggle-option ${locale === 'en' ? 'is-active' : ''}`}
          onClick={() => setLocale('en')}
          aria-label={t('lang.englishTitle')}
          aria-pressed={locale === 'en'}
        >
          <BritishFlag />
          <span>English</span>
        </button>
        <button
          type="button"
          title={t('lang.italianTitle')}
          className={`lang-toggle-option ${locale === 'it' ? 'is-active' : ''}`}
          onClick={() => setLocale('it')}
          aria-label={t('lang.italianTitle')}
          aria-pressed={locale === 'it'}
        >
          <ItalianFlag />
          <span>Italiano</span>
        </button>
      </div>
    </div>
  );
}
