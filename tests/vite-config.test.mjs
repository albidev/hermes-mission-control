import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { loadConfigFromFile } from 'vite';

const configFile = fileURLToPath(new URL('../vite.config.ts', import.meta.url));
const baseEnv = { ...process.env };

async function probe() {
  const result = await loadConfigFromFile(
    { command: 'serve', mode: 'development' },
    configFile,
    process.cwd(),
  );
  const target = result.config.server.proxy['/api'].target;
  const expected = process.env.EXPECTED_DASHBOARD_TARGET;
  assert.equal(target, expected);
}

if (process.env.VITE_CONFIG_CASE) {
  try {
    await probe();
    if (process.env.VITE_CONFIG_CASE === 'invalid') {
      console.error('Vite accepted an invalid dashboard port');
      process.exit(1);
    }
  } catch (error) {
    if (process.env.VITE_CONFIG_CASE !== 'invalid') throw error;
    assert.match(String(error), /invalid dashboard port/);
  }
} else {
  const cases = [
    {
      name: 'derived dashboard target',
      variables: {
        MISSION_CONTROL_DASHBOARD_HOST: '127.0.0.2',
        MISSION_CONTROL_DASHBOARD_PORT: '6002',
        HERMES_DASHBOARD_URL: undefined,
      },
      expected: 'http://127.0.0.2:6002',
    },
    {
      name: 'explicit dashboard target override',
      variables: {
        MISSION_CONTROL_DASHBOARD_HOST: '127.0.0.2',
        MISSION_CONTROL_DASHBOARD_PORT: '6002',
        HERMES_DASHBOARD_URL: 'http://127.0.0.9:7000',
      },
      expected: 'http://127.0.0.9:7000',
    },
    ...['not-a-port', '0', '65536', ' 9119 '].map((port) => ({
      name: `invalid dashboard port ${JSON.stringify(port)}`,
      variables: {
        MISSION_CONTROL_DASHBOARD_HOST: '127.0.0.1',
        MISSION_CONTROL_DASHBOARD_PORT: port,
        HERMES_DASHBOARD_URL: undefined,
      },
      expected: '',
      invalid: true,
    })),
  ];

  for (const testCase of cases) {
    const env = { ...baseEnv, VITE_CONFIG_CASE: testCase.invalid ? 'invalid' : 'valid' };
    for (const [key, value] of Object.entries(testCase.variables)) {
      if (value === undefined) delete env[key];
      else env[key] = value;
    }
    env.EXPECTED_DASHBOARD_TARGET = testCase.expected;
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url)], {
      cwd: process.cwd(),
      env,
      encoding: 'utf8',
    });
    assert.equal(result.status, 0, `${testCase.name}: ${result.stderr || result.stdout}`);
  }

  console.log('Vite dashboard config tests passed');
}
