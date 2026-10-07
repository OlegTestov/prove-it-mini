from shop.pricing import order_total


def test_half_over_100():
    assert order_total([(150.0, 1)], "HALF") == 75.0


def test_half_not_applied_at_exactly_100():
    assert order_total([(100.0, 1)], "HALF") == 100.0


def test_codes_are_case_insensitive():
    assert order_total([(150.0, 1)], "half") == 75.0
    assert order_total([(100.0, 1)], "save10") == 90.0
