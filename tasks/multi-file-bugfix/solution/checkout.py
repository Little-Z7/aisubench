"""结算入口：小计 -> 按比例折扣 -> 加税。运行 `python3 checkout.py` 输出总价。"""
from cart import apply_discount, subtotal
from orders import ORDERS
from tax import add_tax

DISCOUNT_PERCENT = 15
TAX_BAND = "standard"


def order_total(items, discount_percent, band):
    amount = subtotal(items)
    amount = apply_discount(amount, discount_percent)
    return add_tax(amount, band)


def main():
    print(f"TOTAL={order_total(ORDERS, DISCOUNT_PERCENT, TAX_BAND):.2f}")


if __name__ == "__main__":
    main()
