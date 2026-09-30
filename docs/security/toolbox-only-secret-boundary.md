# Security hardening: toolbox-only secret boundary in Python bootstrap

**Status:** Items 1, 2, 4 and 5 **delivered 2026-08-23** for the Outlook fields; item 3
(`app_monitor` SMTP) stays open. **Surfaced by:** the Outlook regex+priority sender-routing review
(2026-07-24), where this was confirmed **pre-existing** and **out of scope** for that routing feature.
See [DECISIONS.md](../../DECISIONS.md) (2026-07-24, 2026-08-23) and [ROADMAP.md](../../ROADMAP.md).

## Problem (the state BEFORE the 2026-08-23 delivery — kept for the rationale)

`docs/architecture/zero-trust.md` and `AGENTS.md` stated **"Toolbox = only container with
secrets."** This section describes what that meant on the **Python** side before `access:
"toolbox_only"` existed. Every bullet below still holds for a field that does NOT declare it;
a field that DOES is now skipped by `bootstrap()` and `resolve_all()`, raises on a direct `.get()`,
and (with `allowEnv: false`) refuses the ENV source as well. The Outlook Graph fields declare both;
`app_monitor`'s SMTP password does not, and is item 3 — still open.

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

1. ✅ **Delivered.** A per-field `"access": "toolbox_only"` classification in `system.json`,
   distinct from `obscure`, plus `"allowEnv": false` for the ENV half. Both are enforced in **both**
   resolvers (Python and the toolbox's JS mirror). Applied to `outlook_client_secret`,
   `outlook_cert_pem` and `outlook_cert_password`.
2. ✅ **Delivered.** `resolve_field` returns `None` (source `toolbox_only`) for such a field, so
   `bootstrap`/`resolve_module_config` never decrypt it on any Python path — cron, consumer reload or
   CLI. A *direct* `ScopedConfigService.get()` on one **raises** (`ToolboxOnlyConfigError`: a direct
   read is a bug), while the bulk `resolve_all()` walk skips it. The ENV source is refused separately
   by `allowEnv: false`, so `CONFIG__OUTLOOK__OUTLOOK_CLIENT_SECRET` cannot smuggle the value back in,
   and `module:validate` fails a deploy that sets one.
3. ⬜ **Open.** Migrate `app_monitor`'s cron-side SMTP breach transport to a **toolbox-owned
   transport** (do not restore Python-side SMTP decryption). This is why the boundary is enforced
   per field rather than for every `obscure` field at once.
4. ✅ **Delivered (documentation half).** `zero-trust.md` now states exactly what is enforced
   (`toolbox_only` fields) and what is not (other `obscure` fields, the mounted `secrets.env`, the
   plaintext `CONFIG__*` path), instead of claiming a blanket invariant. The compose mount itself
   stays until item 3 removes the last cron-side consumer.
5. ✅ **Delivered.** `tests/unit/framework/test_toolbox_only_config.py` proves the decryptor is
   never invoked for a `toolbox_only` field on either source, that a direct `get()` raises, that the
   bulk `resolve_all()` omits the path rather than raising, and that an ENV key cannot smuggle the
   value back in. The JS mirror is covered in `src/agento/toolbox/tests/`. `app_monitor`'s SMTP path
   is untouched and still works (item 3).

## Note

The bitbucket module's agent_view-scoping precedent (store the credential at `agent_view` scope so
`bootstrap`'s DEFAULT-only query never decrypts it) is a good per-module pattern but does not
generalize — e.g. Outlook polls a shared mailbox *before* knowing the agent_view.
