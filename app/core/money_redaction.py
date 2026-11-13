"""Remove financial values from API payloads for callers without money-view rights."""

SENSITIVE_MONEY_KEYS = {
    "unit_cost", "total_cost", "cost_amount", "original_cost",
    "remaining_cost", "avg_unit_cost", "cost", "planned_cost_price",
    "planned_total_cost", "actual_total_cost", "unit_price", "amount",
    "profit", "total_amount", "purchase_price",
}


def redact_money_fields(value):
    if isinstance(value, dict):
        return {
            key: redact_money_fields(item)
            for key, item in value.items()
            if key not in SENSITIVE_MONEY_KEYS
        }
    if isinstance(value, list):
        return [redact_money_fields(item) for item in value]
    if isinstance(value, tuple):
        return [redact_money_fields(item) for item in value]
    return value
