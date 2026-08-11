from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from .screens.agents import AgentsScreen
from .screens.config import ConfigScreen
from .screens.credentials import CredentialsScreen
from .screens.dashboard import DashboardScreen
from .screens.jobs import JobsScreen
from .screens.skills import SkillsScreen
from .screens.tools import ToolsScreen
from .widgets.sidebar import Sidebar

CSS_PATH = Path(__file__).parent / "styles" / "admin.tcss"


class AdminApp(App):
    TITLE = "Agento Admin"
    CSS_PATH = CSS_PATH

    BINDINGS = [  # noqa: RUF012
        Binding("r", "refresh", "r Refresh", show=True),
        Binding("q", "quit", "q Quit", show=True),
        Binding("ctrl+x", "quit", "^X Quit", show=True, priority=True),
    ]

    SCREENS = {  # noqa: RUF012
        "dashboard": DashboardScreen,
        "jobs": JobsScreen,
        "credentials": CredentialsScreen,
        "agents": AgentsScreen,
        "skills": SkillsScreen,
        "tools": ToolsScreen,
        "config": ConfigScreen,
    }

    conn = None

    def on_mount(self) -> None:
        self._connect_db()
        self.push_screen("dashboard")

    def _connect_db(self) -> None:
        try:
            from ..database_config import DatabaseConfig
            from ..db import get_connection

            config = DatabaseConfig.from_env()
            self.conn = get_connection(config)
        except Exception:
            self.conn = None

    def action_refresh(self) -> None:
        screen = self.screen
        if hasattr(screen, "action_refresh"):
            screen.action_refresh()

    def on_sidebar_navigate(self, message: Sidebar.Navigate) -> None:
        self._navigate(message.screen)

    def _navigate(self, screen: str) -> None:
        self.switch_screen(screen)
        for sidebar in self.screen.query(Sidebar):
            sidebar.set_active(screen)
