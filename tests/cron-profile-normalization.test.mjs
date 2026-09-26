import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const source = readFileSync(new URL('../src/lib/hermes-api.ts', import.meta.url), 'utf8');
const file = ts.createSourceFile('hermes-api.ts', source, ts.ScriptTarget.Latest, true);
const normalizer = file.statements.find((statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === 'normalizeCronJob');
assert.ok(normalizer, 'Cron normalizer must exist');
const { outputText } = ts.transpileModule(normalizer.getText(file), { compilerOptions: { target: ts.ScriptTarget.ES2022 } });
const normalizeCronJob = new Function(`${outputText}; return normalizeCronJob;`)();

const scoped = normalizeCronJob({ id: 'job-123', label: 'delivery', profile: 'crossnection-delivery' });
assert.equal(scoped.profile, 'crossnection-delivery', 'the profile from the API must survive snapshot and detail normalization');
const scheduled = normalizeCronJob({ id: 'one-shot', scheduleKind: 'once', scheduleExpr: null, scheduleRunAt: '2026-09-27T10:30:00+02:00' });
assert.equal(scheduled.scheduleRunAt, '2026-09-27T10:30:00+02:00', 'the original one-shot run_at must survive snapshot normalization');
assert.equal(normalizeCronJob({ id: 'default-job' }).profile, 'default', 'legacy jobs without profile belong to default');

const route = readFileSync(new URL('../src/routes/CronRoute.tsx', import.meta.url), 'utf8');
const routeFile = ts.createSourceFile('CronRoute.tsx', route, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const modal = routeFile.statements.find((statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === 'CronFormModal');
assert.ok(modal);
const editProfile = modal.getText(routeFile).match(/<select[^>]*value=\{form\.profile\}[^>]*>/);
assert.ok(editProfile, 'the edit form should show the job owner profile');
assert.match(editProfile[0], /disabled=\{Boolean\(job\)\}/, 'the edit form must not imply that changing a job owner is supported');
console.log('cron profile normalization tests passed');
