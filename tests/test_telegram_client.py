"""Unit tests for src/integrations/telegram_client.py public helpers.

Currently covers ``get_chat_id`` env-var validation: missing, valid integer,
and the non-integer case that previously leaked a bare ``ValueError`` from
``int()`` instead of an ``EnvironmentError`` with a clear diagnostic.
"""

from unittest.mock import patch

import pytest

from src.integrations import telegram_client


def test_get_chat_id_returns_int_when_env_var_is_valid_integer():
    with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}, clear=False):
        assert telegram_client.get_chat_id() == 12345


def test_get_chat_id_raises_environment_error_when_env_var_missing():
    # clear=True wipes the env so TELEGRAM_CHAT_ID is unset regardless of
    # whatever the developer has in their shell.
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(EnvironmentError) as exc_info:
            telegram_client.get_chat_id()
    assert "TELEGRAM_CHAT_ID" in str(exc_info.value)
    assert "Missing" in str(exc_info.value)


def test_get_chat_id_raises_environment_error_for_non_integer_value():
    """Non-numeric TELEGRAM_CHAT_ID must raise EnvironmentError, not ValueError.

    Misconfiguration should look the same regardless of whether the var is
    missing or malformed — both are setup problems, both deserve the same
    diagnostic surface.
    """
    with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "not-an-int"}, clear=False):
        with pytest.raises(EnvironmentError) as exc_info:
            telegram_client.get_chat_id()
    msg = str(exc_info.value)
    assert "TELEGRAM_CHAT_ID" in msg
    assert "integer" in msg.lower()
    # The bad value should be visible to make debugging trivial.
    assert "not-an-int" in msg
