"""购物车金额计算。"""


def subtotal(items):
    """items 里每个元素是含 price / qty 的 dict，返回 price * qty 的合计。"""
    return sum(item["price"] * item["qty"] for item in items)


def apply_discount(amount, percent):
    """按比例打折：percent=15 表示便宜 15%，即返回 amount * (1 - 15/100)。"""
    return amount * (1 - percent / 100)
