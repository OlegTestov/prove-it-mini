from shop.pricing import order_total


def test_total():
    assert order_total([(10.0, 2), (5.0, 1)]) == 25.0


def test_save10():
    assert order_total([(100.0, 1)], "SAVE10") == 90.0
