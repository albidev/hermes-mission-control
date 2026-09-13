#!/usr/bin/env node
/**
 * Run EVERY frontend test file in `tests/`, so a new suite cannot silently stay
 * unexecuted. The curated `test:*` scripts are kept for their existing callers, but they
 * are opt-in inventories: a test file missing from those lists ran in no lane at all, and
 * CI stayed green while the file rotted.
 *
 * Files are discovered by glob, never by list. Each runs as its own process (the repo
 * mixes `node:test` suites with plain assert-and-exit scripts). `--import` registers the
 * resolve hook that lets Node import the app's extensionless specifiers, so suites that
 * reach into `src/lib/*` can actually run.
 *
 * `tests/frontend-baseline.json` lists pre-existing failures. Those are tolerated but
 * never silent: a listed file that PASSES fails the run, so an entry cannot outlive its
 * bug, and a file that is neither listed nor passing fails the run, so new breakage
 * cannot hide inside the baseline. An empty baseline means the lane is fully green.
 *
 * Usage: node scripts/run-frontend-tests.mjs [--filter=<substring>] [--verbose]
 */
import { readdir, readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TESTS_DIR = join(ROOT, 'tests');
const BASELINE_PATH = join(TESTS_DIR, 'frontend-baseline.json');

const filterArg = process.argv.find((arg) => arg.startsWith('--filter='));
const filter = filterArg ? filterArg.slice('--filter='.length) : '';
const verbose = process.argv.includes('--verbose');

let known = {};
try {
  const parsed = JSON.parse(await readFile(BASELINE_PATH, 'utf8'));
  known = parsed.known_failing ?? {};
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}

const entries = await readdir(TESTS_DIR, { withFileTypes: true });
const files = entries
  .filter((entry) => entry.isFile())
  .map((entry) => entry.name)
  .filter((name) => /\.test\.(ts|mts|mjs|js)$/.test(name))
  .filter((name) => !filter || name.includes(filter))
  .sort();

if (files.length === 0) {
  console.error('run-frontend-tests: no test files matched');
  process.exit(1);
}

const runOne = (file) => new Promise((resolve) => {
  const child = spawn(
    process.execPath,
    ['--experimental-strip-types', '--import', './scripts/test-resolve-hooks.mjs', join('tests', file)],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { output += chunk; });
  child.on('close', (code) => resolve({ file, code, output }));
});

const passing = [];
const failing = [];

for (const file of files) {
  const result = await runOne(file);
  if (result.code === 0) {
    passing.push(file);
    if (filter || verbose) console.log(`  ok   ${file}`);
    continue;
  }
  failing.push(result);
  if (filter || verbose) console.error(`  FAIL ${file} (exit ${result.code})`);
}

const baselineStillFailing = failing.filter((result) => known[result.file]).map((result) => result.file);
const regressions = failing.filter((result) => !known[result.file]);
const staleEntries = Object.keys(known).filter((file) => files.includes(file) && passing.includes(file));

console.log(`\nrun-frontend-tests: ${passing.length}/${files.length} files passed`);
if (baselineStillFailing.length > 0) {
  console.log(`  baseline (known failing, not blocking): ${baselineStillFailing.length}`);
  for (const file of baselineStillFailing) console.log(`    ${file}`);
}
if (staleEntries.length > 0) {
  console.error(`\nBaseline entries that now PASS — remove them from tests/frontend-baseline.json:`);
  for (const file of staleEntries) console.error(`  ${file}`);
}

if (regressions.length > 0) {
  console.error(`\n${regressions.length} NEW failing file(s):\n`);
  for (const regression of regressions) {
    const tail = regression.output.trim().split('\n').slice(-14).join('\n');
    console.error(`--- ${regression.file} ---\n${tail}\n`);
  }
}

if (regressions.length > 0 || staleEntries.length > 0) process.exit(1);
