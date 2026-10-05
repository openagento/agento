# admin

Core module that holds the panel display settings: the date format and the time zone of every
timestamp the panel shows. It has no Python, no tools and no `sequence`. The panel Config screen
shows its fields under the **Admin** tab.

| File | Contents |
|---|---|
| `module.json` | no tools (`tools: []`), no `sequence` |
| `config.json` / `system.json` | `admin/locale/date_format`, `admin/locale/timezone` |

## Config

Both fields are `showInDefault` only: the panel is global.

| Path | Default | Options | Meaning |
|---|---|---|---|
| `admin/locale/date_format` | `us` | `us` (10/5/2026, 8:23:44 AM), `eu` (5.10.2026, 8:23:44 AM), `iso` (2026-10-05 08:23:44) | How the panel shows a date and time |
| `admin/locale/timezone` | `browser` | `browser`, then every IANA zone (`options_source: timezones`) | The zone the panel shows a time in; `browser` uses the zone of each viewer's browser |

```bash
bin/agento config:set admin/locale/date_format eu
bin/agento config:set admin/locale/timezone Europe/Warsaw
```

`web` resolves both values at the default scope (ENV → DB → `config.json`, the Config screen's
resolver) and returns them as `display` in the session ([panel.md](../architecture/panel.md#session)).
A value that is not one of the field's options gets the `config.json` default. The panel formats
every `Timestamp` with them; a time zone that the browser does not know shows the browser's zone.

## Disabling the module

With `admin` disabled, `display` is `null` and the panel shows each time in the browser's locale
(`toLocaleString()`), as before the module existed.
