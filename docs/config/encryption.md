# Encryption

Fields marked as `"type": "obscure"` in module.json are encrypted when stored in `core_config_data`.

## Setup

The encryption key is generated automatically during `bin/agento install`:

```env
# In secrets.env
AGENTO_ENCRYPTION_KEY=<64-char hex string>
```

If you need to set it manually:
```bash
echo "AGENTO_ENCRYPTION_KEY=$(openssl rand -hex 32)" >> ../secrets.env
```

## How It Works

1. You invoke `config:set` **without** a value argument (so the secret never hits `ps aux` or shell history):
   ```bash
   bin/agento config:set my_app/tools/mysql_prod/pass
   # Paste value…  <Ctrl+D>
   # or: echo -n "$SECRET" | bin/agento config:set my_app/tools/mysql_prod/pass
   ```
2. Python checks if `pass` field is `obscure` in module.json.
3. If yes → encrypts with AES-256-CBC → stores as `aes256:{iv_hex}:{ciphertext_hex}` with `encrypted=1`.
4. Toolbox reads DB at runtime → decrypts using the same `AGENTO_ENCRYPTION_KEY`.

> **Known limitation:** the Python `bootstrap()` also transiently decrypts DEFAULT-scope `obscure`
> values in the cron/consumer/CLI while resolving module config. For *toolbox-only* fields (e.g. the
> Outlook Graph secret) this is unnecessary and the value is discarded unused; but `app_monitor`
> intentionally consumes its obscure SMTP password cron-side (to send breach alerts), so not every
> obscure field is unused Python-side. Making Python bootstrap never decrypt toolbox-only fields
> (and migrating the app_monitor SMTP transport) is tracked separately — see
> [toolbox-only secret boundary](../security/toolbox-only-secret-boundary.md).

> **Don't** pass secrets as the positional `value` arg (`config:set path my-secret`). That leaks the value into `ps aux` for the duration of the command and into `~/.bash_history` / `~/.zsh_history` forever. Always omit the value so agento prompts / reads stdin. See [cli/config.md](../cli/config.md#secrets--never-pass-on-the-command-line) for the full rationale.

## Algorithm

- **Cipher:** AES-256-CBC
- **Key derivation:** SHA-256 of `AGENTO_ENCRYPTION_KEY`
- **IV:** Random 16 bytes per encryption
- **Padding:** PKCS7
- **Format in DB:** `aes256:{iv_hex}:{ciphertext_hex}`

## Cross-Language Compatibility

Both Python and Node.js implementations produce/consume the same format:

| Language | File | Used By |
|----------|------|---------|
| Python | [src/agento/framework/crypto.py](../../src/agento/framework/crypto.py) | `config:set` (encrypt) |
| Node.js | [docker/toolbox/crypto.js](../../docker/toolbox/crypto.js) | Config loader (decrypt) |

## Which Fields Are Encrypted

Any field with `"type": "obscure"` in module.json:

```json
{
  "fields": {
    "pass": {"type": "obscure", "label": "Password"},
    "host": {"type": "string", "label": "Host"}
  }
}
```

Only `pass` would be encrypted. `host` is stored as plain text.

## Key Rotation

Currently manual: re-encrypt all values with a new key by reading + re-setting each obscure config value after changing `AGENTO_ENCRYPTION_KEY`.
