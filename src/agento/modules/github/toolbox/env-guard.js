// src/agento/modules/github/toolbox/env-guard.js
// The JS mirror of src/env_guard.py. Keep the two key lists identical — the guard test in Task 9
// asserts that (it parses the key names out of THIS file and compares them to the Python tuple),
// because two drifting copies of
// a security boundary are worse than one.
export const VIEW_SCOPED_ENV_KEYS = [
  'CONFIG__GITHUB__GITHUB_TOKEN',
  'CONFIG__GITHUB__GITHUB_LOGIN',
  'CONFIG__GITHUB__REPO_ALLOWLIST',
];

export function offendingEnvKeys(env = process.env) {
  return VIEW_SCOPED_ENV_KEYS.filter((key) => env[key]);
}
