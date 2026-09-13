/**
 * Resolve hook that lets `node --experimental-strip-types` import the app's
 * extensionless relative specifiers.
 *
 * `src/` is authored for Vite, which resolves `./foo` to `./foo.ts`. Node's type
 * stripping requires the extension to be written out, so any test that reaches
 * into `src/lib/*` transitively dies with ERR_MODULE_NOT_FOUND — which is why
 * whole suites sat in no lane rather than being fixed. Resolving here keeps the
 * application sources untouched (a Vite-owned concern) and makes the test lane
 * follow the bundler's own resolution order.
 *
 * Loaded with `node --import ./scripts/test-resolve-hooks.mjs`. A hooks module
 * must REGISTER itself (`registerHooks`/`register`); merely importing it via
 * `--import` does not install it.
 */
import { existsSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';

const CANDIDATE_SUFFIXES = ['.ts', '.tsx', '.mts', '/index.ts', '/index.tsx'];

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (!specifier.startsWith('.') && !specifier.startsWith('/')) {
      return nextResolve(specifier, context);
    }
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (error?.code !== 'ERR_MODULE_NOT_FOUND') throw error;
      const base = specifier.startsWith('/')
        ? new URL(`file://${specifier}`)
        : new URL(specifier, context.parentURL);
      const basePath = fileURLToPath(base);
      for (const suffix of CANDIDATE_SUFFIXES) {
        const candidate = basePath + suffix;
        if (existsSync(candidate)) {
          return nextResolve(base.href + suffix, context);
        }
      }
      throw error;
    }
  },
});
