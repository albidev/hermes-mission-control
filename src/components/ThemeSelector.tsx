import { useMissionControl } from '../lib/mission-control-store';

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" className="theme-icon" aria-hidden>
      <circle cx="12" cy="12" r="4.2" fill="currentColor" />
      <path d="M12 2.5v2.3M12 19.2v2.3M4.8 4.8l1.7 1.7M17.5 17.5l1.7 1.7M2.5 12h2.3M19.2 12h2.3M4.8 19.2l1.7-1.7M17.5 6.5l1.7-1.7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className="theme-icon" aria-hidden>
      <path
        d="M14.8 3.3a8.9 8.9 0 1 0 5.9 14.9A9.6 9.6 0 0 1 14.8 3.3Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ThemeSelector() {
  const { resolvedTheme, setTheme } = useMissionControl();

  return (
    <div className="theme-toggle" role="group" aria-label="Theme mode">
      <span className="theme-toggle-label">Theme</span>
      <div className="theme-toggle-track">
        <button
          type="button"
          title="Light theme"
          className={`theme-toggle-option ${resolvedTheme === 'light' ? 'is-active' : ''}`}
          onClick={() => setTheme('light')}
          aria-label="Enable light theme"
          aria-pressed={resolvedTheme === 'light'}
        >
          <SunIcon />
          <span>Light</span>
        </button>
        <button
          type="button"
          title="Dark theme"
          className={`theme-toggle-option ${resolvedTheme === 'dark' ? 'is-active' : ''}`}
          onClick={() => setTheme('dark')}
          aria-label="Enable dark theme"
          aria-pressed={resolvedTheme === 'dark'}
        >
          <MoonIcon />
          <span>Dark</span>
        </button>
      </div>
    </div>
  );
}
