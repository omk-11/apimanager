"""
Sample target repo: a small billing service that uses the Stripe SDK.

This file intentionally exercises every change category introduced between
spec_old.json and spec_new.json so that the pipeline has real call sites to
find, patch, and test:

  - source → payment_method rename        (CreateCharge request field)
  - customer.balance integer → number      (type change on response field)
  - payment_method newly required          (CreatePaymentIntent request)
  - /v1/charges POST deprecated            (deprecated endpoint)
  - charge.source response-attribute read  (should be left alone / SKIPPED)
"""
import os

import stripe

stripe.api_key = os.environ.get("STRIPE_API_KEY", "sk_test_placeholder")


# ---------------------------------------------------------------------------
# Charges (deprecated endpoint — Stage 1 will flag /v1/charges POST)
# ---------------------------------------------------------------------------

def create_charge(amount: int, currency: str, source_token: str) -> dict:
    """
    Create a charge using the legacy charges API.
    Uses `source=` which has been renamed to `payment_method=` in the new spec.
    """
    charge = stripe.Charge.create(
        amount=amount,
        currency=currency,
        source=source_token,          # RENAME: source → payment_method
        description="Order payment",
    )
    return {
        "id":     charge.id,
        "status": charge.status,
        # Response-side read — Stage 3 must NOT delete this line
        "source": charge.source,      # SKIPPED: response-side field read
    }


def get_charge_outcome(charge_id: str) -> str:
    charge = stripe.Charge.retrieve(charge_id)
    # Another response-side attribute read — must be preserved
    return charge.outcome.seller_message or ""


# ---------------------------------------------------------------------------
# PaymentIntents (preferred path)
# ---------------------------------------------------------------------------

def create_payment_intent(amount: int, currency: str) -> dict:
    """
    Create a PaymentIntent.
    `payment_method` is newly required in the new spec — Stage 3 must add it.
    """
    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency=currency,
        # payment_method is missing here — Stage 3 / LLM fallback should add it
        confirm=False,
    )
    return {
        "id":            intent.id,
        "client_secret": intent.client_secret,
        "status":        intent.status,
    }


def confirm_payment_intent(intent_id: str, payment_method: str) -> dict:
    intent = stripe.PaymentIntent.confirm(
        intent_id,
        payment_method=payment_method,
    )
    return {"id": intent.id, "status": intent.status}


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def create_customer(email: str, name: str) -> dict:
    customer = stripe.Customer.create(email=email, name=name)
    return {
        "id":    customer.id,
        "email": customer.email,
        # balance was integer in old spec; now it's a number (float) — type change
        "balance": customer.balance,  # TYPE CHANGE: integer → number
    }


def get_customer_balance(customer_id: str) -> float:
    customer = stripe.Customer.retrieve(customer_id)
    # Response-side balance read — must not be deleted
    return float(customer.balance)   # TYPE CHANGE: callers may need cast
