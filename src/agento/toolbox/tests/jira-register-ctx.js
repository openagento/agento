import { vi } from 'vitest';

// The register() context the jira toolbox module needs, with a configured fake site.
export function jiraCtx(isToolEnabled = () => true) {
  return {
    log: vi.fn(),
    moduleConfigs: { jira: { jira_host: 'https://x.atlassian.net', jira_user: 'u@x.com', jira_token: 't' } },
    isToolEnabled,
    artifactsDir: '/tmp/art',
    fileManager: { downloadAndConvert: vi.fn() },
  };
}
