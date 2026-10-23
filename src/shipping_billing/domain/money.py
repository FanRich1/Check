"""Exact decimal arithmetic, with explicit per-currency rounding only."""

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext


def precision(*values):
    return max(
        64, sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent) for v in values if v is not None) + 16
    )


def add(values):
    values = list(values)
    with localcontext() as ctx:
        ctx.prec = precision(*values)
        return sum(values, Decimal(0))


def product(a, b):
    with localcontext() as ctx:
        ctx.prec = precision(a, b)
        return a * b


def quantized(value, minor_units, rounding="HALF_EVEN"):
    with localcontext() as ctx:
        ctx.prec = precision(value)
        return value.quantize(
            Decimal(1).scaleb(-minor_units),
            rounding={"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}[rounding],
        )


def expected(value, minor_units, rounding):
    return value if rounding == "exact" else quantized(value, minor_units, rounding)
