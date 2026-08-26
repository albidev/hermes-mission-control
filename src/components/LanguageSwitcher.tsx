import { useI18n } from '../lib/i18n';

function BritishFlag() {
  return (
    <svg viewBox="0 0 24 24" className="lang-icon" aria-hidden>
      <clipPath id="union-jack-clip"><rect x="2" y="5" width="20" height="14" rx="1.5" /></clipPath>
      <g clipPath="url(#union-jack-clip)">
        <rect x="2" y="5" width="20" height="14" fill="#012169" />
        <path d="M2 5l20 14M22 5L2 19" stroke="#fff" strokeWidth="4" />
        <path d="M2 5l20 14M22 5L2 19" stroke="#c8102e" strokeWidth="2" />
        <path d="M12 5v14M2 12h20" stroke="#fff" strokeWidth="5" />
        <path d="M12 5v14M2 12h20" stroke="#c8102e" strokeWidth="2.5" />
      </g>
      <rect x="2" y="5" width="20" height="14" rx="1.5" fill="none" stroke="currentColor" strokeWidth="0.8" />
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
