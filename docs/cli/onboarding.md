# Module Onboarding

When `setup:upgrade` detects a module with incomplete onboarding (e.g. missing API credentials), it presents a strict interactive flow. There are three ways to handle onboarding:

## 1. Interactive (default)

Run `agento setup:upgrade` without flags. For each module needing onboarding:

1. You are prompted to **Proceed with onboarding** or **Skip (choose action)**.
2. If onboarding completes successfully, the module is marked as onboarded.
3. If onboarding is incomplete (e.g. invalid credentials), you choose:
   - **Retry** -- re-run the onboarding flow
   - **Disable** -- disable the module and all its dependents
   - **Quit** -- abort `setup:upgrade` entirely

Arrow-key navigation is used when running in a TTY terminal. In non-TTY environments (CI pipes), a numbered fallback is displayed.

When a module is disabled, all modules that transitively depend on it are also disabled automatically.

## 2. CI/CD (skip onboarding)

```bash
agento setup:upgrade --skip-onboarding
```

Bypasses all onboarding prompts. Useful for CI/CD pipelines where interactive input is not available. Modules with incomplete onboarding will remain enabled but unconfigured -- ensure config values are set beforehand via `config:set` or ENV variables.

## 3. Manual (pre-configure)

Set the required config values before running `setup:upgrade`:

```bash
agento config:set jira/jira_host https://mycompany.atlassian.net
agento config:set jira/jira_user user@example.com
# Token is an obscure field — omit the value so agento prompts instead of
# leaving it in your shell history. Paste the token, press Ctrl+D.
agento config:set jira/jira_token
agento config:set jira/jira_assignee_account_id <account_id>
agento config:set jira/jira_projects '["PROJECT_KEY"]'
agento setup:upgrade
```

When `is_complete()` finds all required values present, the onboarding prompt is skipped automatically for that module.

## Channel-specific onboarding

Some channels add their own verify-before-save onboarding and completeness rules:

- **Outlook** — Graph app credentials (client secret or certificate PEM). See [docs/modules/outlook.md](../modules/outlook.md).
- **Bitbucket** — workspace, agent account, API token (verified against `GET /2.0/user` inside the toolbox before anything is saved) and watched repos. Bitbucket config is **always agent_view-scoped** (the API token is never stored at DEFAULT scope, so it is only ever decrypted in the toolbox); onboarding auto-selects the sole active view or prompts when there are several, and refuses if there are none. Completeness requires the token + `bitbucket_account_uuid` + `repo_allowlist` at the owning view's agent_view scope. See [docs/modules/bitbucket.md](../modules/bitbucket.md) for the manual `config:set` path.
- **GitHub** — owner, agent account personal access token (verified against `GET /user` inside the toolbox before anything is saved) and watched repos; the account's `login` comes from the verification, never from typing. The token, `github_login` and `repo_allowlist` are written at the owning view's own agent_view scope (`config:set` refuses them at DEFAULT/WORKSPACE), so the token is only ever decrypted in the toolbox; onboarding auto-selects the sole active view or prompts when there are several, and refuses if there are none. Completeness requires those three at the owning view's agent_view scope, and reports **incomplete** while any of `CONFIG__GITHUB__GITHUB_TOKEN`, `CONFIG__GITHUB__GITHUB_LOGIN` or `CONFIG__GITHUB__REPO_ALLOWLIST` is set as a global ENV override (a global override would resolve one shared identity for every view). See [docs/modules/github.md](../modules/github.md) for the manual `config:set` path.
