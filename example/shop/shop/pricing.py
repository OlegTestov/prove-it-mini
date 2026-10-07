def order_total(items, discount_code=None):
    """items: list of (unit_price, qty). Returns the total after an optional discount code."""
    subtotal = sum(price * qty for price, qty in items)
    if discount_code == "SAVE10":
        return round(subtotal * 0.9, 2)
    return round(subtotal, 2)
