# Security hardening: toolbox-only secret boundary in Python bootstrap

**Status:** Backlog (design note). **Surfaced by:** the Outlook regex+priority sender-routing
review (2026-07-24), where this was confirmed **pre-existing** and **out of scope** for that
routing feature. See [DECISIONS.md](../../DECISIONS.md) (2026-07-24) and
[ROADMAP.md](../../ROADMAP.md).

## Problem

`docs/architecture/zero-trust.md` and `AGENTS.md` state **"Toolbox = only container with
secrets."** In practice this is an aspiration, not an enforced invariant, on the **Python** side:

- `bootstrap()` → `resolve_module_config()` resolves **every** declared field of **every** enabled
  module, and `config_resolver` **decrypts** any DEFAULT-scope `obscure` field
  (`config_resolver.py:46,168`). So the **cron** publisher, the **consumer** (every reload —
  `consumer.py:199`), and the **CLI** all transiently decrypt secrets like the Outlook Graph creds,
  even though they never use them (the registry discards them after `from_dict`).
- The **ENV** path is unprotected regardless: `entrypoint.sh` persists every `CONFIG__*` var into
  the cron environment, and the 3-level config fallback reads ENV before the DB, so a secret
  supplied as `CONFIG__<MODULE>__<FIELD>` is plaintext in the process.
- `docker-compose*.yml` mounts `secrets.env` into the cron service, directly contradicting the
  zero-trust doc.
- The boundary is **already** crossed by design elsewhere: `app_monitor` reads an `obscure` SMTP
  password from bootstrap config and sends authenticated SMTP **from the cron** (via the
  `security_breach_after` observer). So a blanket "skip all obscure fields in Python bootstrap"
  would silently break breach alerts.

## Why it needs its own effort (not a routing-PRD change)

A correct fix is framework-wide and multi-module — it cannot be bolted onto a channel feature, and
a naive per-caller opt-out is both incomplete (consumer + ENV) and harmful (breaks app_monitor).

## Proposed scope

1. A new per-field classification **`toolbox_only`** in `system.json`, **distinct from `obscure`**
   (Outlook Graph creds and app_monitor SMTP are both `obscure` today, so a single flag can't
   separate "the toolbox needs it" from "a Python process legitimately needs it").
2. `bootstrap`/`resolve_module_config` **never resolve `toolbox_only` fields** — across **all**
   Python callers (cron, consumer reload, CLI), covering **both** the DB and the ENV `CONFIG__*`
   source.
3. Migrate `app_monitor`'s cron-side SMTP breach transport to a **toolbox-owned transport** (do not
   restore Python-side SMTP decryption).
4. Fix the doc/compose drift (`zero-trust.md` vs `secrets.env` mounted into cron).
5. Regression tests proving the decryptor is never invoked for `toolbox_only` fields on any Python
   bootstrap path (DB and ENV), and that legitimately-needed secrets (e.g. app_monitor SMTP) still
   work via the toolbox transport.

## Note

The bitbucket module's agent_view-scoping precedent (store the credential at `agent_view` scope so
`bootstrap`'s DEFAULT-only query never decrypts it) is a good per-module pattern but does not
generalize — e.g. Outlook polls a shared mailbox *before* knowing the agent_view.
