from decimal import Decimal

from odds_scanner.presentation import decimal_to_american


def test_decimal_to_american():
    assert decimal_to_american(Decimal("2.10")) == 110
    assert decimal_to_american(Decimal("1.50")) == -200
