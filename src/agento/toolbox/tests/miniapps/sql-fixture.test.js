import { it, expect } from 'vitest';
import fs from 'node:fs';
import { ACTIVATION_SQL, ACTIVATE_SQL, DEACTIVATE_SQL } from '../../../modules/miniapps/toolbox/miniapps.js';
import { LAUNCH_SQL } from '../../../modules/miniapps/toolbox/launch-source.js';

// The integration test runs these exact statements against the real schema.
const FIXTURE = JSON.parse(fs.readFileSync(new URL('../../../../../tests/fixtures/miniapp_sql_v1.json', import.meta.url), 'utf8'));

it('runs the SQL the integration test runs against the real schema', () => {
  expect({ ACTIVATION_SQL, ACTIVATE_SQL, DEACTIVATE_SQL, LAUNCH_SQL }).toEqual({
    ACTIVATION_SQL: FIXTURE.activation_sql, ACTIVATE_SQL: FIXTURE.activate_sql,
    DEACTIVATE_SQL: FIXTURE.deactivate_sql, LAUNCH_SQL: FIXTURE.launch_sql,
  });
});
