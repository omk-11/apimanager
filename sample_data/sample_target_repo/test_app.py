"""
Tests for the sample target repo.

These tests are deliberately simple and do not make real Stripe API calls —
they mock the stripe SDK so they pass locally without credentials.
Stage 4 will run this file with pytest.
"""
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: build fake Stripe response objects
# ---------------------------------------------------------------------------

def _fake_charge(**kwargs):
    obj = MagicMock()
    obj.id = "ch_test_123"
    obj.status = "succeeded"
    obj.source = {"id": "card_test", "brand": "Visa"}
    obj.outcome = MagicMock()
    obj.outcome.seller_message = "Payment complete."
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _fake_intent(**kwargs):
    obj = MagicMock()
    obj.id = "pi_test_456"
    obj.client_secret = "pi_test_456_secret"
    obj.status = "requires_confirmation"
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _fake_customer(**kwargs):
    obj = MagicMock()
    obj.id = "cus_test_789"
    obj.email = "test@example.com"
    obj.balance = 0
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


# ---------------------------------------------------------------------------
# Charge tests
# ---------------------------------------------------------------------------

@patch("stripe.Charge.create", return_value=_fake_charge())
def test_create_charge_returns_id(mock_create):
    from app import create_charge
    result = create_charge(1000, "usd", "tok_visa")
    assert result["id"] == "ch_test_123"
    assert result["status"] == "succeeded"


@patch("stripe.Charge.create", return_value=_fake_charge())
def test_create_charge_includes_source(mock_create):
    """Response-side source field must survive patching."""
    from app import create_charge
    result = create_charge(1000, "usd", "tok_visa")
    assert "source" in result


@patch("stripe.Charge.retrieve", return_value=_fake_charge())
def test_get_charge_outcome(mock_retrieve):
    from app import get_charge_outcome
    msg = get_charge_outcome("ch_test_123")
    assert msg == "Payment complete."


# ---------------------------------------------------------------------------
# PaymentIntent tests
# ---------------------------------------------------------------------------

@patch("stripe.PaymentIntent.create", return_value=_fake_intent())
def test_create_payment_intent_returns_secret(mock_create):
    from app import create_payment_intent
    result = create_payment_intent(2000, "usd")
    assert result["client_secret"] == "pi_test_456_secret"


@patch(
    "stripe.PaymentIntent.confirm",
    return_value=_fake_intent(status="succeeded"),
)
def test_confirm_payment_intent(mock_confirm):
    from app import confirm_payment_intent
    result = confirm_payment_intent("pi_test_456", "pm_card_visa")
    assert result["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Customer tests
# ---------------------------------------------------------------------------

@patch("stripe.Customer.create", return_value=_fake_customer())
def test_create_customer(mock_create):
    from app import create_customer
    result = create_customer("test@example.com", "Test User")
    assert result["id"] == "cus_test_789"
    assert result["balance"] == 0


@patch("stripe.Customer.retrieve", return_value=_fake_customer(balance=500))
def test_get_customer_balance(mock_retrieve):
    from app import get_customer_balance
    balance = get_customer_balance("cus_test_789")
    assert balance == 500.0
    assert isinstance(balance, float)
