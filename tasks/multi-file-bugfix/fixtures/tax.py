"""税费计算。税率表 TAX_RATES 是正确的，不要改动。"""

TAX_RATES = {"standard": 0.08, "reduced": 0.05, "exempt": 0.0}


def add_tax(amount, band):
    """按档位 band 加税：返回 amount * (1 + TAX_RATES[band])；档位未知时抛 ValueError。"""
    if band not in TAX_RATES:
        raise ValueError(f"unknown tax band: {band}")
    rate = TAX_RATES[band + "_rate"]
    return amount * (1 + rate)
