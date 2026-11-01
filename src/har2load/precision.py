"""Exact, resource-bounded arithmetic for interpreted timing (not raw JSON).

Source decimals are unrounded regardless of this interpretation budget. Timing
outside 1000 significant/fractional digits or float-finite magnitude is diagnosed
as unavailable rather than rounded or expanded without bounds.
"""

import math
from decimal import Decimal, InvalidOperation, localcontext


def timing_number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not math.isfinite(float(number)):
            return None
        parts = number.as_tuple()
        if len(parts.digits) > 1000 or abs(int(parts.exponent)) > 1000:
            return None
        return number
    except (ValueError, OverflowError, InvalidOperation):
        return None


def exact_sum(left: Decimal, right: Decimal) -> Decimal:
    """No binary-float conversion and no epsilon at the pause threshold."""
    exponent = min(int(left.as_tuple().exponent), int(right.as_tuple().exponent))
    precision = max(32, max(left.adjusted(), right.adjusted()) - exponent + 3)
    with localcontext() as context:
        context.prec = precision
        return left + right


def seconds_from_milliseconds(value: Decimal) -> Decimal:
    parts = value.as_tuple()
    return Decimal((parts.sign, parts.digits, int(parts.exponent) - 3))
