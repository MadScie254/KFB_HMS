"""Stock statistics derived from the movement ledger.

Every figure here is an aggregate over ``StockMovement`` and the posted billing
records. Nothing is stored as a running total, so a number on a screen can
always be traced back to the movements that produced it. Where a figure cannot
be computed honestly — a product with no purchase cost recorded, for example —
the caller is told how many rows were excluded rather than being handed a total
that quietly understates the position.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    CatalogueItem,
    GoodsReceipt,
    Invoice,
    InvoiceLine,
    PriceVersion,
    StockBatch,
    StockMovement,
)

ZERO_MONEY = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))
ZERO_QUANTITY = Value(Decimal("0.000"), output_field=DecimalField(max_digits=14, decimal_places=3))

DEFAULT_EXPIRY_WINDOW_DAYS = 90


def active_price_map():
    """Current approved unit price per catalogue item, in one query.

    Reading the active price per row inside a loop is one query per product;
    on a real catalogue that is the difference between a page and a stall.
    """
    now = timezone.now()
    prices = (
        PriceVersion.objects.filter(effective_from__lte=now)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
        .order_by("item_id", "-effective_from")
        .values_list("item_id", "amount")
    )
    latest = {}
    for item_id, amount in prices:
        latest.setdefault(item_id, amount)
    return latest


def batch_rows(expiry_window_days=DEFAULT_EXPIRY_WINDOW_DAYS):
    """Every batch with its ledger balance, cost value and expiry standing."""
    today = timezone.localdate()
    horizon = today + timedelta(days=expiry_window_days)
    prices = active_price_map()
    batches = (
        StockBatch.objects.select_related("item")
        .annotate(on_hand=Coalesce(Sum("movements__quantity_delta"), ZERO_QUANTITY))
        .order_by("item__name", "expiry_date", "batch_number")
    )
    rows = []
    for batch in batches:
        on_hand = batch.on_hand or Decimal("0.000")
        unit_price = prices.get(batch.item_id)
        expired = bool(batch.expiry_date) and batch.expiry_date < today
        rows.append({
            "batch": batch,
            "item": batch.item,
            "on_hand": on_hand,
            "cost_value": (on_hand * batch.purchase_cost_per_base_unit).quantize(Decimal("0.01")),
            "retail_value": (on_hand * unit_price).quantize(Decimal("0.01")) if unit_price is not None else None,
            "unit_price": unit_price,
            "expired": expired,
            "near_expiry": bool(batch.expiry_date) and not expired and batch.expiry_date <= horizon,
            "days_to_expiry": (batch.expiry_date - today).days if batch.expiry_date else None,
            "quarantined": batch.status == StockBatch.Status.QUARANTINE,
            "sellable": batch.can_dispense,
        })
    return rows


def stock_position(expiry_window_days=DEFAULT_EXPIRY_WINDOW_DAYS):
    """Valuation and risk position of everything currently on the shelf.

    Retail value covers only the products that carry an approved price; the
    count of the rest is returned so the figure can be shown with its own
    caveat instead of pretending the catalogue is fully priced.
    """
    rows = batch_rows(expiry_window_days)
    products = {}
    cost_value = Decimal("0.00")
    retail_value = Decimal("0.00")
    unpriced_items = set()

    for row in rows:
        item = row["item"]
        bucket = products.setdefault(item.pk, {
            "item": item,
            "on_hand": Decimal("0.000"),
            "cost_value": Decimal("0.00"),
            "retail_value": Decimal("0.00"),
            "unit_price": row["unit_price"],
            "batch_count": 0,
            "sellable_on_hand": Decimal("0.000"),
            "earliest_expiry": None,
            "has_expired": False,
            "has_near_expiry": False,
            "priced": row["unit_price"] is not None,
        })
        bucket["on_hand"] += row["on_hand"]
        bucket["cost_value"] += row["cost_value"]
        bucket["batch_count"] += 1
        if row["sellable"]:
            bucket["sellable_on_hand"] += row["on_hand"]
        if row["retail_value"] is not None:
            bucket["retail_value"] += row["retail_value"]
        if row["expired"]:
            bucket["has_expired"] = True
        if row["near_expiry"]:
            bucket["has_near_expiry"] = True
        expiry = row["batch"].expiry_date
        if expiry and row["on_hand"] > 0 and (bucket["earliest_expiry"] is None or expiry < bucket["earliest_expiry"]):
            bucket["earliest_expiry"] = expiry

        cost_value += row["cost_value"]
        if row["retail_value"] is not None:
            retail_value += row["retail_value"]
        elif row["on_hand"] > 0:
            unpriced_items.add(item.pk)

    product_rows = []
    for bucket in products.values():
        item = bucket["item"]
        bucket["reorder_level"] = item.reorder_level
        bucket["below_reorder"] = bucket["sellable_on_hand"] <= item.reorder_level
        bucket["shortfall"] = max(Decimal("0.000"), item.reorder_level - bucket["sellable_on_hand"])
        product_rows.append(bucket)
    product_rows.sort(key=lambda row: row["item"].name)

    below_reorder = [row for row in product_rows if row["below_reorder"]]
    expiring = sorted(
        (row for row in rows if row["near_expiry"] and row["on_hand"] > 0),
        key=lambda row: row["batch"].expiry_date,
    )
    expired = [row for row in rows if row["expired"] and row["on_hand"] > 0]
    quarantined = [row for row in rows if row["quarantined"] and row["on_hand"] > 0]

    return {
        "rows": rows,
        "products": product_rows,
        "stock_value_cost": cost_value,
        "stock_value_retail": retail_value,
        "potential_margin": retail_value - cost_value,
        "unpriced_item_count": len(unpriced_items),
        "below_reorder": below_reorder,
        "below_reorder_count": len(below_reorder),
        "expiring_soon": expiring,
        "expiring_soon_count": len(expiring),
        "expired": expired,
        "expired_count": len(expired),
        "expired_value": sum((row["cost_value"] for row in expired), Decimal("0.00")),
        "quarantined": quarantined,
        "quarantined_count": len(quarantined),
        "batch_count": len(rows),
        "product_count": len(product_rows),
        "expiry_window_days": expiry_window_days,
    }


def stock_activity(days=7):
    """What moved in the period, and what the movement was worth.

    Cost of goods dispensed prices each outward movement at the purchase cost of
    the batch it came from, so it reconciles to the same rows the balances do.
    """
    start = timezone.now() - timedelta(days=days)
    movements = StockMovement.objects.filter(event_at__gte=start).select_related("batch__item")

    received_units = Decimal("0.000")
    received_value = Decimal("0.00")
    dispensed_units = Decimal("0.000")
    cost_of_goods = Decimal("0.00")
    adjustment_units = Decimal("0.000")
    adjustment_value = Decimal("0.00")
    movers = {}

    for movement in movements:
        cost = movement.batch.purchase_cost_per_base_unit
        delta = movement.quantity_delta
        if movement.movement_type == StockMovement.MovementType.RECEIPT:
            received_units += delta
            received_value += (delta * cost).quantize(Decimal("0.01"))
        elif movement.movement_type in {
            StockMovement.MovementType.DISPENSE,
            StockMovement.MovementType.CONSUMPTION,
        }:
            out = -delta
            dispensed_units += out
            cost_of_goods += (out * cost).quantize(Decimal("0.01"))
            bucket = movers.setdefault(movement.batch.item_id, {"item": movement.batch.item, "units": Decimal("0.000"), "cost": Decimal("0.00")})
            bucket["units"] += out
            bucket["cost"] += (out * cost).quantize(Decimal("0.01"))
        elif movement.movement_type == StockMovement.MovementType.ADJUSTMENT:
            adjustment_units += delta
            adjustment_value += (delta * cost).quantize(Decimal("0.01"))

    sales_value = InvoiceLine.objects.filter(
        item__kind=CatalogueItem.Kind.PRODUCT,
        invoice__posted_at__gte=start,
    ).exclude(invoice__status=Invoice.Status.DRAFT).aggregate(
        value=Coalesce(Sum("line_total"), ZERO_MONEY)
    )["value"]

    top_movers = sorted(movers.values(), key=lambda row: row["units"], reverse=True)[:8]

    return {
        "days": days,
        "received_units": received_units,
        "received_value": received_value,
        "dispensed_units": dispensed_units,
        "cost_of_goods_dispensed": cost_of_goods,
        "adjustment_units": adjustment_units,
        "adjustment_value": adjustment_value,
        "product_sales_value": sales_value,
        "product_gross_margin": sales_value - cost_of_goods,
        "top_movers": top_movers,
        "movement_count": len(movements),
    }


def receiving_summary(days=30):
    """Delivery control position: what was received and what remains unchecked."""
    start = timezone.now() - timedelta(days=days)
    receipts = GoodsReceipt.objects.filter(delivered_at__gte=start)
    return {
        "receipt_count": receipts.count(),
        "unchecked_count": receipts.filter(checked_by__isnull=True).count(),
        "invoiced_value": receipts.aggregate(value=Coalesce(Sum("invoice_amount"), ZERO_MONEY))["value"],
        "without_photo": receipts.filter(invoice_photo="").count(),
    }
