"""A small suite to run on the cluster. Two tests fail on purpose so the results view has something to show."""

import time


def order_total(items, tax_rate=0.18):
    return round(sum(price * qty for price, qty in items) * (1 + tax_rate), 2)


def test_total_includes_tax():
    assert order_total([(100, 1)]) == 118


def test_total_of_several_items():
    assert order_total([(10, 2), (5, 4)]) == 47.2


def test_empty_order_costs_nothing():
    assert order_total([]) == 0


def test_checkout_checkout_slowly():
    time.sleep(1.5)  # long enough to watch the run in the dashboard
    assert order_total([(1, 1)], tax_rate=0) == 1


def test_negative_quantity_is_rejected():
    # Known bug: a negative quantity gives a negative total instead of an error.
    assert order_total([(10, -1)]) >= 0


def test_discount_codes():
    raise AssertionError("discount codes are not implemented yet")
