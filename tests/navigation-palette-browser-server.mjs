// Sacrificial mounted-shell fixture: real MissionControlShell + real ChatDrawer,
// synthetic API and a stub WebSocket, no live proxy and no user data.
//
// Mirrors tests/provider-usage-browser-server.mjs: a Vite dev server serves a
// synthetic entry that mounts the production components, while a plain HTTP
// server answers the /api/local surface and completes the /api/ws upgrade.
import { createServer as createHttpServer } from 'node:http';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import tailwind from '@tailwindcss/postcss';
import autoprefixer from 'autoprefixer';

const require = createRequire(import.meta.url);
const [runtime, sourceArgument] = process.argv.slice(2);
const root = resolve(sourceArgument || process.cwd());
const requests = [];
const upgrades = [];
const entryId = '\0shell-palette-entry.tsx';
const storeId = '\0shell-palette-store';
const aliases = ['react', 'react/jsx-runtime', 'react/jsx-dev-runtime', 'react-dom', 'react-dom/client', 'react-router-dom', 'lucide-react', 'react-markdown', 'remark-gfm', 'remark-breaks']
  .map((name) => ({ find: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`), replacement: require.resolve(name) }));
aliases.push({ find: /^tailwindcss$/, replacement: require.resolve('tailwindcss/index.css') });

const vite = await createServer({
  root, configFile: false, appType: 'custom', logLevel: 'error',
  cacheDir: resolve(runtime, 'vite-cache'),
  resolve: { alias: aliases, dedupe: ['react', 'react-dom'] },
  css: { postcss: { plugins: [tailwind(), autoprefixer()] } },
  plugins: [{
    name: 'shell-palette-fixture', enforce: 'pre',
    resolveId(id) {
      if (id === '/shell-palette-entry.tsx') return entryId;
      // The shell, ThemeSelector, PushToggle and NavStatusIndicator all read the
      // production store. A fixture store keeps authRequired false and the token
      // synthetic without standing up the real telemetry backend.
      if (id.includes('mission-control-store')) return storeId;
    },
    load(id) {
      if (id === storeId) return `
        const store = window.__mcFixtureStore;
        export const useMissionControl = () => store;
        export const MissionControlProvider = ({ children }) => children;
      `;
      if (id === entryId) return `
        import React from 'react';
        import { createRoot } from 'react-dom/client';
        import { MemoryRouter, Route, Routes, useLocation, useOutletContext } from 'react-router-dom';
        import { useEffect } from 'react';
        import { I18nProvider } from ${JSON.stringify(resolve(root, 'src/lib/i18n.tsx'))};
        import { ToastProvider } from ${JSON.stringify(resolve(root, 'src/components/ui/ToastProvider.tsx'))};
        import { MissionControlShell } from ${JSON.stringify(resolve(root, 'src/components/MissionControlShell.tsx'))};
        import { QuickActions } from ${JSON.stringify(resolve(root, 'src/components/overview/QuickActions.tsx'))};
        import { AgentStatusBar } from ${JSON.stringify(resolve(root, 'src/components/overview/AgentStatusBar.tsx'))};
        import ${JSON.stringify(resolve(root, 'src/styles.css'))};

        function LocationProbe() {
          const location = useLocation();
          return React.createElement('div', { 'data-testid': 'location', hidden: true }, location.pathname + location.search);
        }
        function Page({ name }) {
          const { registerCurrentViewRefresh } = useOutletContext();
          useEffect(() => {
            if (!['/', '/cron'].includes(name)) return;
            const unregisterPrimary = registerCurrentViewRefresh('fixture-' + name, () => {
              window.__mcRefreshCalls.push(name);
              if (window.__mcSyncRefreshFailure) throw new Error('fixture synchronous refresh failure');
              return new Promise(resolve => setTimeout(resolve, 220)).then(() => {
                if (window.__mcRefreshFailure) throw new Error('fixture refresh failure');
              });
            });
            window.__mcRegisterExtraRefresh = () => {
              window.__mcUnregisterExtraRefresh?.();
              window.__mcUnregisterExtraRefresh = registerCurrentViewRefresh('fixture-extra', async () => {
                window.__mcRefreshCalls.push('extra');
              });
            };
            return () => {
              unregisterPrimary();
              window.__mcUnregisterExtraRefresh?.();
              delete window.__mcRegisterExtraRefresh;
              delete window.__mcUnregisterExtraRefresh;
            };
          }, [name, registerCurrentViewRefresh]);
          return React.createElement('div', { 'data-testid': 'page' }, name === '/' ? React.createElement(AgentStatusBar) : name,
            name === '/' ? React.createElement('section', { className: 'widget-quick-actions' },
              React.createElement(QuickActions, {
                gatewayActions: window.__mcFixtureStore.gatewayActions,
                runGatewayAction: window.__mcFixtureStore.runGatewayAction,
                actionLoading: window.__mcFixtureStore.actionLoading,
                restartAvailable: true,
              })) : null);
        }

        let tree = null;
        function render(options) {
          const pluginItems = options.pluginItems || [];
          const registry = { getNavItems: () => pluginItems };
          const routes = [{ path: 'cron', name: '/cron' }]
            .concat(pluginItems.map((item) => ({ path: item.to.replace(/^\\//, ''), name: item.to })));
          return React.createElement(MemoryRouter, { initialEntries: ['/'] },
            React.createElement(I18nProvider, null,
              React.createElement(ToastProvider, null,
                React.createElement(LocationProbe),
                React.createElement(Routes, null,
                  React.createElement(Route, { element: React.createElement(MissionControlShell, { registry, navItems: pluginItems }) },
                    React.createElement(Route, { index: true, element: React.createElement(Page, { name: '/' }) }),
                    routes.map((route) => React.createElement(Route, { key: route.path, path: route.path, element: React.createElement(Page, { name: route.name }) })),
                  ),
                ),
              ),
            ),
          );
        }
        window.mountShell = (options = {}) => {
          try { window.sessionStorage.removeItem('mission-control-chat-open'); } catch {}
          try { window.localStorage.removeItem('mission-control-last-route'); window.sessionStorage.removeItem('mission-control-last-route'); } catch {}
          if (!tree) tree = createRoot(document.getElementById('root'));
          tree.render(render(options));
          window.__mcShellOptions = options;
        };
        window.unmountShell = () => { if (tree) { tree.unmount(); tree = null; } };
        window.mountShell();
      `;
    },
  }, react()],
  server: { middlewareMode: true, hmr: false, ws: { host: '127.0.0.1' }, cors: false, fs: { allow: [root, process.cwd()] } },
});

const injection = `
  window.failures = []; window.requests = []; window.consoleErrors = [];
  window.__mcActionCalls = []; window.__mcRefreshCalls = [];
  window.__mcRestartFailureMode = null; window.__mcRestartFailure = false;
  window.__mcRefreshFailure = false; window.__mcSyncRefreshFailure = false;
  window.__mcActionFailure = false;
  window.__mcFixtureStore = {
    authRequired: false, authError: null, loading: false, storedToken: 'fixture-only',
    snapshot: { gatewayStatus: 'online', activeAgents: 1, activeModel: 'fixture-model @ fixture-provider', fallbackModel: 'fixture-fallback', alerts: { items: [] }, sessions: { items: [], totalSessions: 0, activeAgents: 0 }, cron: { items: [], queuedJobs: 0 }, machine: { source: 'fixture' } },
    tools: { available: false }, skills: { available: false }, config: { available: false },
    tokenDraft: '', setTokenDraft: () => {}, unlock: async () => {}, logout: () => {},
    refreshAll: async () => {}, reloadConfig: async () => ({}), saveConfig: async () => ({}),
    runGatewayAction: async (action) => {
      window.__mcActionCalls.push(action.id);
      await new Promise(resolve => setTimeout(resolve, 220));
      if (window.__mcActionFailure && action.id === 'refresh') throw Object.assign(new Error('fixture refresh authentication failure'), { name: 'MissionControlAuthError' });
      if (window.__mcRestartFailureMode === 'disconnect') throw new TypeError('fixture network disconnect');
      if (window.__mcRestartFailureMode === 'timeout') throw Object.assign(new Error('fixture request timeout'), { name: 'TimeoutError' });
      if (window.__mcRestartFailure) throw new Error('fixture restart failure');
    },
    gatewayActions: [
      { id: 'refresh', label: 'Refresh', endpoint: '/api/local/refresh', method: 'POST' },
      { id: 'restart-gateway', label: 'Restart gateway', endpoint: '/api/local/gateway/restart', method: 'POST' },
    ], actionResult: null, actionLoading: null,
    linkStatus: null, setLinkStatus: () => {}, lastUpdatedAt: null,
    theme: 'dark', setTheme: () => {}, resolvedTheme: 'dark',
  };
  addEventListener('error', event => window.failures.push(String(event.message)));
  addEventListener('unhandledrejection', event => window.failures.push(String(event.reason)));
  // Only identity metadata is answered in-browser; chat keeps its existing
  // local upgrade-only fixture. No profile/session data comes from the live.
  window.__mcPrimaryName = 'Primary Agent';
  window.WebSocket = class extends WebSocket {
    send(data) {
      const request = JSON.parse(data);
      if (request.method !== 'profiles.list') return super.send(data);
      queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({
        jsonrpc: '2.0', id: request.id, result: { profiles: [
          { name: 'builder', display_name: 'Unrelated Bot' },
          { name: 'default', is_default: true, display_name: window.__mcPrimaryName },
        ] },
      }) })));
    }
  };
  const errorOriginal = console.error.bind(console);
  console.error = (...args) => { window.consoleErrors.push(args.map(String).join(' ')); errorOriginal(...args); };
  const fetchOriginal = window.fetch.bind(window);
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin !== location.origin) throw new Error('Fixture forbids external requests');
    const response = await fetchOriginal(input, options);
    window.requests.push({ path: url.pathname + url.search, method: options.method || 'GET', status: response.status });
    return response;
  };
`;

const json = (res, status, payload) => {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(payload));
};

const server = createHttpServer(async (req, res) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  if (path.startsWith('/api/')) {
    requests.push({ path, method: req.method });
    if (path === '/api/gateway-root') {
      res.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
      res.end('<html><body>fixture gateway root</body></html>');
      return;
    }
    if (path === '/api/auth/ws-ticket' && req.method === 'POST') return json(res, 200, { ticket: 'fixture-ticket' });
    if (path === '/api/local/client-diagnostics') return json(res, 200, { accepted: true });
    // Push acceptance uses synthetic browser subscriptions, never real permission or delivery.
    if (path === '/api/local/push/vapid-public-key') return json(res, 200, { publicKey: 'AQID' });
    if (path === '/api/local/push/subscriptions' && ['POST', 'DELETE'].includes(req.method)) return json(res, 200, { accepted: true });
    // Every other reference endpoint answers "absent" rather than 401, so the
    // fixture store keeps authRequired false and the shell stays unlocked.
    return json(res, 404, { error: 'Unknown fixture route' });
  }
  if (path === '/sw.js') {
    res.writeHead(200, { 'Content-Type': 'text/javascript', 'Cache-Control': 'no-store' });
    res.end('self.addEventListener("install", () => self.skipWaiting());');
    return;
  }
  if (path === '/fixture/state') return json(res, 200, { requests, upgrades: upgrades.length });
  if (path === '/' || path === '/index.html') {
    res.setHeader('Content-Type', 'text/html');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws:; img-src 'self' data:; font-src 'self'");
    const html = `<html class="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script>${injection}</script><script type="module" src="/shell-palette-entry.tsx"></script></body></html>`;
    // This fixture has no HMR upgrade handler; do not inject a client that
    // opens an unrelated socket and reports a delayed connection failure.
    res.end((await vite.transformIndexHtml('/', html)).replace(/<script type="module" src="\/@vite\/client"><\/script>/, ''));
    return;
  }
  vite.middlewares(req, res, () => { res.writeHead(404); res.end(); });
});

// Minimal RFC6455 accept for /api/ws: enough for the drawer to reach
// connectionState === "connected" (and therefore an enabled composer). Incoming
// frames are ignored; the fixture never answers RPCs.
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
server.on('upgrade', (req, socket) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  const key = req.headers['sec-websocket-key'];
  if (path !== '/api/ws' || !key) { socket.destroy(); return; }
  const accept = createHash('sha1').update(key + WS_GUID).digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
    + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`);
  socket.on('error', () => {});
  socket.on('close', () => {});
  upgrades.push(socket);
});

server.listen(0, '127.0.0.1', () => writeFileSync(resolve(runtime, 'port'), String(server.address().port)));
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(signal, async () => {
  for (const socket of upgrades) socket.destroy();
  server.close();
  await vite.close();
  process.exit(0);
});
