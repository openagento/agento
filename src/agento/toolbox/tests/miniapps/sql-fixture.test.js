import { it, expect } from 'vitest';
import fs from 'node:fs';
import { LAUNCH_SQL, retentionPins } from '../../../modules/miniapps/toolbox/launch-source.js';

// The integration test runs these exact statements against the real schema.
const FIXTURE = JSON.parse(fs.readFileSync(new URL('../../../../../tests/fixtures/miniapp_sql_v1.json', import.meta.url), 'utf8'));

it('runs the SQL the integration test runs against the real schema', () => {
  expect({ LAUNCH_SQL, PINS: retentionPins }).toEqual({
    LAUNCH_SQL: FIXTURE.launch_sql, PINS: [FIXTURE.retention_pin_sql],
  });
});
