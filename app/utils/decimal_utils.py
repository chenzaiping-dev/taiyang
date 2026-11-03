from decimal import Decimal, ROUND_HALF_UP

Q2 = Decimal("0.01")   # DECIMAL(18,6)
Q3 = Decimal("0.001")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
RATIO_TOLERANCE = Decimal("0.000001")   # 配比合计容差

def _q3(v):
    return Decimal(str(v)).quantize(Q3, rounding=ROUND_HALF_UP)

def _q6(v):
    return Decimal(str(v)).quantize(Q4, rounding=ROUND_HALF_UP)

def _q6_amount(v) -> Decimal:
    return Decimal(str(v)).quantize(Q4, rounding=ROUND_HALF_UP)