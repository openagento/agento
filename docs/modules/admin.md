# admin

Core module that holds the panel display settings: the default language, and the date format and
the time zone of every timestamp the panel shows. It has no Python, no tools and no `sequence`. The panel Config screen
shows its fields under the **Admin** tab.

| File | Contents |
|---|---|
| `module.json` | no tools (`tools: []`), no `sequence` |
| `config.json` / `system.json` | `admin/locale/language`, `admin/locale/date_format`, `admin/locale/timezone` |

## Config

The three fields are `showInDefault` only: the panel is global.

| Path | Default | Options | Meaning |
|---|---|---|---|
| `admin/locale/language` | `en` | every registered language (`options_source: panel_languages`) | The default language of the panel; a user can set another language for the account |
| `admin/locale/date_format` | `us` | `us` (10/5/2026, 8:23:44 AM), `eu` (5.10.2026, 8:23:44 AM), `iso` (2026-10-05 08:23:44) | How the panel shows a date and time |
| `admin/locale/timezone` | `browser` | `browser`, then every IANA zone (`options_source: timezones`) | The zone the panel shows a time in; `browser` uses the zone of each viewer's browser |

```bash
bin/agento config:set admin/locale/language pl
bin/agento config:set admin/locale/date_format eu
bin/agento config:set admin/locale/timezone Europe/Warsaw
```

`web` resolves both values at the default scope (ENV → DB → `config.json`, the Config screen's
resolver) and returns them as `display` in the session ([panel.md](../architecture/panel.md#session)).
A value that is not one of the field's options gets the `config.json` default. The options and the
`config.json` defaults are read once, when `web` starts; ENV and the database are read for each
session answer. A `config.json` default that is not one of its field's options is not used: `display`
is then `null`, as with `admin` disabled, and `web` logs one line. The panel formats
every `Timestamp` with them; a time zone that the browser does not know is written in UTC, and the label ends with `UTC`.

## Language

`admin/locale/language` is the instance language: the language of the sign-in screen, of a new
account, and of each account that has no stored language. `web` reads it at the default scope
(ENV `CONFIG__ADMIN__LOCALE__LANGUAGE` → DB → `config.json`). A value that the language registry
does not have gives `en`.

- A user sets the language of the own account in the profile dialog, which the user name in the panel header opens (`PATCH /api/profile/locale`).
  The account keeps it in `user.ui_locale`, and it wins over the instance language.
- `user:create` stores the instance language in the new account.
- `setup:upgrade` stores the instance language in each account that has none (after the SQL
  migrations, before the data patches). A later change of `admin/locale/language` does not change a
  stored account language.
- A change of `admin/locale/language` does not change the language of an open panel. A session
  with no stored account language gets the new language at its next start (a page load or a
  sign-in): the session answer then carries the new `locale.effective`.
- The options are the registered languages: `en` and `pl` ship with the framework, and an enabled
  language pack adds its own ([i18n.md](../development/i18n.md)).
- While neither ENV nor the database sets `admin/locale/date_format`, the panel formats a date by the
  active language (`display.date_format_explicit` is `false`).

## Disabling the module

With `admin` disabled, `display` is `null` and the panel uses the shipped display settings.
`web` reads the enablement of `admin` for each session answer, so `module:disable admin` applies
at the next request with no restart. The options and file defaults are read at start: an `admin`
module that was disabled when `web` started needs a restart after `module:enable admin`. The
instance language follows the catalogs: it reads the module set at start, so restart `web` for it. These
are two different things:

- **Date format:** it follows the active panel language, not the browser's language. English
  shows the `us` format; any other language writes a date in its own way.
- **Time zone:** the browser's time zone.

The instance language is `en`; a user can still set the language of the own account, and that
language then decides the date format.
