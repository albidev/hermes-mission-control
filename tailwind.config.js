/** @type {import('tailwindcss').Config} */
// Tailwind 4 is CSS-first: the theme lives in `@theme inline` in src/styles.css.
// This file is kept only to pin the content globs explicitly.
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
}
