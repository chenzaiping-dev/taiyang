from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def format_qty(value):
    """Format a quantity for display only; never use this for calculations or form values."""
    if value is None or value == "":
        return "0.00"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        # Preserve placeholders such as '-' used by comparison/detail templates.
        return str(value)
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = Decimal("0.00")
    return format(rounded, ".2f")

def format_cost(value):
    """Format a quantity for display only; never use this for calculations or form values."""
    if value is None or value == "":
        return "0.00"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        # Preserve placeholders such as '-' used by comparison/detail templates.
        return str(value)
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = Decimal("0.00")
    return format(rounded, ".2f")
