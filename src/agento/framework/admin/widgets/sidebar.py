from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class Sidebar(Widget):

    DEFAULT_CSS = """
    Sidebar {
        width: 22;
        background: $surface;
        border-right: solid $primary;
        padding: 0;
    }
    Sidebar .nav-title {
        padding: 1;
        text-style: bold;
        color: $text;
    }
    Sidebar OptionList {
        background: $surface;
        border: none;
        padding: 0;
        height: 1fr;
    }
    Sidebar OptionList:focus {
        border: none;
    }
    Sidebar OptionList > .option-list--option-highlighted {
        background: $primary 40%;
        text-style: bold;
    }
    Sidebar OptionList > .option-list--option-hover {
        background: $primary 20%;
    }
    """

    ITEMS = [  # noqa: RUF012
        ("dashboard", "  Dashboard"),
        ("jobs", "  Jobs"),
        ("agents", "  Agents"),
        ("credentials", "  Credentials"),
        ("skills", "  Skills"),
        ("tools", "  Tools"),
        ("config", "  Config"),
    ]

    class Navigate(Message):
        def __init__(self, screen: str) -> None:
            self.screen = screen
            super().__init__()

    def __init__(self, active: str = "dashboard", **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial_active = active

    def compose(self) -> ComposeResult:
        yield Static("Agento Admin", classes="nav-title")
        yield OptionList(
            *[Option(label, id=key) for key, label in self.ITEMS],
            id="nav-list",
        )

    def on_mount(self) -> None:
        nav = self.query_one("#nav-list", OptionList)
        for i, (key, _) in enumerate(self.ITEMS):
            if key == self._initial_active:
                nav.highlighted = i
                break

    def set_active(self, screen: str) -> None:
        nav = self.query_one("#nav-list", OptionList)
        for i, (key, _) in enumerate(self.ITEMS):
            if key == screen:
                nav.highlighted = i
                break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.post_message(self.Navigate(event.option_id))
