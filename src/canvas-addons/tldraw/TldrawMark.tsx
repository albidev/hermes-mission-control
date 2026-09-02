/**
 * Lightweight standalone TLDraw mark — no tldraw editor dependencies.
 * Used in the addon manifest icon without breaking lazy loading.
 */
export function TldrawMark({ size = 16 }: { size?: number }) {
  return (
    <span
      className="tldraw-brand-icon"
      aria-hidden="true"
      style={{ width: size, height: size, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
    >
      <svg viewBox="0 0 32 32" width={size} height={size} fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="4" y="4" width="24" height="24" rx="4" stroke="currentColor" strokeWidth="2" />
        <path d="M10 16 L22 16 M16 10 L16 22" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
    </span>
  );
}
