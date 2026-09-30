"""Bounds for operational thresholds that control clinical and ledger alerts."""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

# These are guardrails, not recommended operating values. Each site still
# confirms its own thresholds before production use.
NUMERIC_SETTING_RULES = {
    "near_expiry_days": (Decimal("0"), Decimal("3650"), True),
    "purchase_cost_variance_fraction": (Decimal("0"), Decimal("1"), False),
    "supplier_invoice_tolerance": (Decimal("0"), Decimal("100000"), False),
    "stock_variance_review_value": (Decimal("0"), Decimal("1000000"), False),
}


def validated_setting_decimal(key, value):
    """Return a finite in-range Decimal or raise a clear configuration error."""
    minimum, maximum, integer_only = NUMERIC_SETTING_RULES[key]
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Setting {key} must be a number between {minimum} and {maximum}.") from exc
    if not number.is_finite() or number < minimum or number > maximum:
        raise ValidationError(f"Setting {key} must be a finite number between {minimum} and {maximum}.")
    if integer_only and number != number.to_integral_value():
        raise ValidationError(f"Setting {key} must be a whole number of days.")
    return number
