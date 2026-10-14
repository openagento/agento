from __future__ import annotations

import sys

DEPRECATION_NOTICE = (
    "agento admin is deprecated: use the admin panel. It stays until v1.0.0; the CLI commands stay."
)


class AdminCommand:
    @property
    def name(self) -> str:
        return "admin"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Launch the admin terminal interface (deprecated)"

    def configure(self, parser) -> None:
        pass

    def execute(self, args) -> None:
        from .app import AdminApp

        print(DEPRECATION_NOTICE, file=sys.stderr, flush=True)
        try:
            AdminApp().run()
        finally:
            # Textual can leave SGR mouse reporting on if shutdown is cut short.
            print("\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l", end="", flush=True)
