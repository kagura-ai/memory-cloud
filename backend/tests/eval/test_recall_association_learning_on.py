"""#3023: the eval runners switch recall-time association learning on."""

from config.settings import get_settings
from tests.eval._provisioning import recall_association_learning_on


def test_switches_learning_on_and_restores_the_previous_value(monkeypatch):
    monkeypatch.setattr(get_settings(), "recall_association_learning_enabled", False)

    with recall_association_learning_on():
        assert get_settings().recall_association_learning_enabled is True

    assert get_settings().recall_association_learning_enabled is False
