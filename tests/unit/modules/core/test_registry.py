from __future__ import annotations

import pytest

from agento.framework.channels.base import Channel
from agento.framework.channels.registry import clear, get_channel, register_channel
from agento.modules.jira.src.channel import JiraChannel


@pytest.fixture(autouse=True)
def _clean_registry():
    clear()
    yield
    clear()


class TestRegistry:
    def test_get_jira_channel(self):
        register_channel(JiraChannel())
        ch = get_channel("jira")
        assert ch.name == "jira"
        assert isinstance(ch, Channel)

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown channel"):
            get_channel("unknown")

    def test_register_custom_channel(self):
        class FakeChannel:
            @property
            def name(self) -> str:
                return "fake"

            def get_prompt_fragments(self, reference_id, config=None):
                pass

            def get_followup_fragments(self, reference_id, instructions, config=None):
                pass

        fake = FakeChannel()
        register_channel(fake)
        assert get_channel("fake") is fake
