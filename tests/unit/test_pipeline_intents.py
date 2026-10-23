"""Separate shipping clearance instructions from explicit edits to accounting fields."""

import pytest

from shipping_billing.pipeline.intents import active_hint


@pytest.mark.parametrize(
    "text",
    [
        'PLEASE ENSURE ALL REQUESTS TO “CLEAR AT THE PORT OF DISCHARGE” ARE RECEIVED.',
        "Please clear your cargo with Customs against the following B/L",
        "Please clear the cargo through customs.",
        "Clear through the port of loading.",
        "Please clear customs before release.",
        "Do not clear the payment term.",
    ],
)
def test_customs_clearance_and_negated_edits_are_not_ledger_changes(text):
    assert not active_hint(text)


@pytest.mark.parametrize(
    "text",
    [
        "Please clear the payment term.",
        "Clear the invoice reference number.",
        "Please update amount from 40 to 50.",
        "请清空付款方式。",
        "Clear customs; Update amount from 40 to 50.",
        "Please clear your cargo with Customs and clear the payment term.",
    ],
)
def test_explicit_billing_changes_remain_detectable(text):
    assert active_hint(text)
