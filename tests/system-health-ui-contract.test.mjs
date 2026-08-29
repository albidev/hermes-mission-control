import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(
  new URL('../src/components/overview/SystemHealthPanel.tsx', import.meta.url),
  'utf8',
);

const levelBranch = source.indexOf('machine.thermal.thermalLevel !== null');
const temperatureBranch = source.indexOf('machine.thermal.thermalPressure !== null');

assert.notEqual(levelBranch, -1, 'System health UI must branch on macOS thermal level');
assert.notEqual(temperatureBranch, -1, 'System health UI must branch on Linux temperature');
assert.ok(
  levelBranch < temperatureBranch,
  'Thermal pressure level must take precedence over numeric pressure/temperature',
);
assert.match(source, /level=\{machine\.thermal\.thermalLevel\}/);
assert.match(source, /value=\{machine\.thermal\.thermalLevel/);
assert.match(source, /value=\{`\$\{machine\.thermal\.thermalPressure\.toFixed\(1\)}°C`\}/);

console.log('system health UI contract tests passed');
