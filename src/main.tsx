import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { ensureServiceWorker } from './lib/push-client';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Register the push service worker once, non-blocking. Guarded for HTTPS:
// service worker registration fails silently on insecure origins.
ensureServiceWorker();
