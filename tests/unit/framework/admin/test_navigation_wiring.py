"""Every sidebar entry must name a screen the app actually registers.

The credentials screen was unreachable for a while: ``AdminApp.SCREENS`` was renamed to
``"credentials"`` but the sidebar still emitted ``"tokens"``, so selecting it called
``switch_screen("tokens")`` on a key that no longer existed. Nothing failed at import
time — only at the click. These are cheap structural assertions that catch it.
"""
from __future__ import annotations

from agento.framework.admin.app import AdminApp
from agento.framework.admin.widgets.sidebar import Sidebar


def test_every_sidebar_item_resolves_to_a_registered_screen():
    unknown = [key for key, _label in Sidebar.ITEMS if key not in AdminApp.SCREENS]
    assert unknown == [], (
        f"sidebar targets missing from AdminApp.SCREENS: {unknown}. "
        f"Registered: {sorted(AdminApp.SCREENS)}"
    )


def test_every_registered_screen_is_reachable_from_the_sidebar():
    """A screen nobody can navigate to is dead weight."""
    keys = {key for key, _label in Sidebar.ITEMS}
    assert set(AdminApp.SCREENS) <= keys, (
        f"screens with no sidebar entry: {sorted(set(AdminApp.SCREENS) - keys)}"
    )


def test_each_screen_marks_itself_active_with_its_own_key():
    """``Sidebar(active=...)`` inside a screen must match that screen's registry key, or
    the highlight lands on the wrong row."""
    import inspect
    import re

    for key, screen_cls in AdminApp.SCREENS.items():
        source = inspect.getsource(screen_cls)
        actives = re.findall(r'Sidebar\(active="([a-z_]+)"\)', source)
        for active in actives:
            assert active == key, (
                f"{screen_cls.__name__} (registered as {key!r}) renders "
                f"Sidebar(active={active!r})"
            )


def test_credentials_is_the_navigation_key():
    """Pins the rename itself — the pre-0.15 key was 'tokens'."""
    keys = {key for key, _label in Sidebar.ITEMS}
    assert "credentials" in keys
    assert "tokens" not in keys
