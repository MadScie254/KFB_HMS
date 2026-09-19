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
    DepartmentIssue,
    DepartmentIssueLine,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    PriceVersion,
    StockBatch,
    StockCount,
    StockMovement,
    StockWriteOff,
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


def batch_rows(expiry_window_days=DEFAULT_EXPIRY_WINDOW_DAYS, include_depleted=False):
    """Every batch that still matters, with balance, value and expiry standing.

    A batch that has been fully dispensed is history, not position. Keeping
    depleted batches in the result means every stock screen does more work each
    year the hospital stays open, for rows that are all zero. They are excluded
    unless a caller explicitly asks, and the per-item totals that depend on a
    complete product list are built from the catalogue instead.
    """
    today = timezone.localdate()
    horizon = today + timedelta(days=expiry_window_days)
    prices = active_price_map()
    batches = (
        StockBatch.objects.select_related("item")
        .annotate(on_hand=Coalesce(Sum("movements__quantity_delta"), ZERO_QUANTITY))
        .order_by("item__name", "expiry_date", "batch_number")
    )
    if not include_depleted:
        batches = batches.exclude(on_hand=Decimal("0.000"))
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
    prices = active_price_map()
    cost_value = Decimal("0.00")
    sellable_cost_value = Decimal("0.00")
    retail_value = Decimal("0.00")
    unpriced_items = set()

    def new_bucket(item):
        return {
            "item": item,
            "on_hand": Decimal("0.000"),
            "cost_value": Decimal("0.00"),
            "retail_value": Decimal("0.00"),
            "unit_price": prices.get(item.pk),
            "batch_count": 0,
            "sellable_on_hand": Decimal("0.000"),
            "earliest_expiry": None,
            "has_expired": False,
            "has_near_expiry": False,
            "priced": prices.get(item.pk) is not None,
        }

    # Seed from the catalogue, not from the batches. Building the product list
    # out of stock rows makes a product that has never been received invisible —
    # and a product with nothing on the shelf is the one most urgently needing
    # reorder, so the reorder list was silently missing exactly what it existed
    # to surface.
    products = {
        item.pk: new_bucket(item)
        for item in CatalogueItem.objects.filter(kind=CatalogueItem.Kind.PRODUCT, active=True)
    }

    for row in rows:
        item = row["item"]
        bucket = products.setdefault(item.pk, new_bucket(item))
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
        if row["sellable"]:
            sellable_cost_value += row["cost_value"]
        # Expired and quarantined batches cannot be sold, so they are excluded
        # from the value the hospital could realise. Counting them would report
        # medicine that legally cannot leave the shelf as though it were an asset.
        if row["retail_value"] is not None and row["sellable"]:
            retail_value += row["retail_value"]
        elif row["retail_value"] is None and row["on_hand"] > 0:
            unpriced_items.add(item.pk)

    product_rows = []
    for bucket in products.values():
        item = bucket["item"]
        bucket["reorder_level"] = item.reorder_level
        # A reorder level of zero means nobody set one, not "reorder at zero".
        # Flagging those as short by nothing buries the products that really
        # are short. Running out is still reported, as a stock-out.
        bucket["below_reorder"] = item.reorder_level > 0 and bucket["sellable_on_hand"] <= item.reorder_level
        bucket["out_of_stock"] = bucket["sellable_on_hand"] <= 0
        bucket["shortfall"] = max(Decimal("0.000"), item.reorder_level - bucket["sellable_on_hand"])
        product_rows.append(bucket)
    product_rows.sort(key=lambda row: row["item"].name)

    below_reorder = [row for row in product_rows if row["below_reorder"]]
    out_of_stock = [row for row in product_rows if row["out_of_stock"]]
    expiring = sorted(
        (row for row in rows if row["near_expiry"] and row["on_hand"] > 0),
        key=lambda row: row["batch"].expiry_date,
    )
    expired = [row for row in rows if row["expired"] and row["on_hand"] > 0]
    quarantined = [row for row in rows if row["quarantined"] and row["on_hand"] > 0]

    return {
        "rows": rows,
        "products": product_rows,
        "stock_value_cost": sellable_cost_value,
        "stock_value_all_cost": cost_value,
        "stock_value_unsellable_cost": cost_value - sellable_cost_value,
        "stock_value_retail": retail_value,
        "potential_margin": retail_value - sellable_cost_value,
        "unpriced_item_count": len(unpriced_items),
        "below_reorder": below_reorder,
        "below_reorder_count": len(below_reorder),
        "out_of_stock": out_of_stock,
        "out_of_stock_count": len(out_of_stock),
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

    # A margin needs both halves. Billing a sale that was never dispensed leaves
    # cost at zero, which reads as a 100% margin — worse than no figure at all.
    margin_available = dispensed_units > 0 and sales_value > 0

    return {
        "days": days,
        "received_units": received_units,
        "received_value": received_value,
        "dispensed_units": dispensed_units,
        "cost_of_goods_dispensed": cost_of_goods,
        "adjustment_units": adjustment_units,
        "adjustment_value": adjustment_value,
        "product_sales_value": sales_value,
        "margin_available": margin_available,
        "product_gross_margin": (sales_value - cost_of_goods) if margin_available else None,
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


def departmental_custody():
    """Stock issued to departments and not yet accounted for.

    These units already left the pharmacy balance when custody moved, so the
    hospital still holds them; they are simply not on the pharmacy shelf. The
    total is reported separately rather than folded into stock value, because
    "on the shelf" and "somewhere in the hospital" are different questions.
    """
    lines = (
        DepartmentIssueLine.objects
        .filter(issue__status=DepartmentIssue.Status.OUTSTANDING)
        .select_related("batch__item", "issue")
    )
    rows = []
    total_units = Decimal("0.000")
    total_value = Decimal("0.00")
    by_department = {}
    for line in lines:
        outstanding = line.outstanding
        if outstanding <= 0:
            continue
        value = (outstanding * line.batch.purchase_cost_per_base_unit).quantize(Decimal("0.01"))
        age_days = (timezone.now() - line.issue.issued_at).days
        rows.append({
            "line": line, "issue": line.issue, "item": line.batch.item,
            "outstanding": outstanding, "value": value, "age_days": age_days,
        })
        total_units += outstanding
        total_value += value
        bucket = by_department.setdefault(line.issue.department, {"department": line.issue.department, "units": Decimal("0.000"), "value": Decimal("0.00")})
        bucket["units"] += outstanding
        bucket["value"] += value

    rows.sort(key=lambda row: row["age_days"], reverse=True)
    stale = [row for row in rows if row["age_days"] >= 7]
    return {
        "rows": rows,
        "by_department": sorted(by_department.values(), key=lambda row: row["value"], reverse=True),
        "outstanding_units": total_units,
        "outstanding_value": total_value,
        "issue_count": len({row["issue"].pk for row in rows}),
        "stale_rows": stale,
        "stale_count": len(stale),
        "stale_value": sum((row["value"] for row in stale), Decimal("0.00")),
    }


def shrinkage(days=90):
    """Unexplained stock loss, valued, over a period.

    Only approved count adjustments count as shrinkage. A write-off has a named
    reason and an approver, so it is a loss the hospital decided to take, not an
    unexplained one — the two are reported separately and never summed.
    """
    start = timezone.now() - timedelta(days=days)
    movements = (
        StockMovement.objects
        .filter(event_at__gte=start, movement_type=StockMovement.MovementType.ADJUSTMENT)
        .select_related("batch__item")
    )
    loss_units = Decimal("0.000")
    loss_value = Decimal("0.00")
    gain_units = Decimal("0.000")
    gain_value = Decimal("0.00")
    written_off_value = Decimal("0.00")
    by_item = {}

    for movement in movements:
        cost = movement.batch.purchase_cost_per_base_unit
        value = (abs(movement.quantity_delta) * cost).quantize(Decimal("0.01"))
        if movement.reference_type == "StockWriteOff":
            written_off_value += value
            continue
        if movement.quantity_delta < 0:
            loss_units += -movement.quantity_delta
            loss_value += value
            bucket = by_item.setdefault(movement.batch.item_id, {"item": movement.batch.item, "units": Decimal("0.000"), "value": Decimal("0.00")})
            bucket["units"] += -movement.quantity_delta
            bucket["value"] += value
        else:
            gain_units += movement.quantity_delta
            gain_value += value

    cost_of_goods = stock_activity(days)["cost_of_goods_dispensed"]
    as_percent_of_cogs = (
        (loss_value / cost_of_goods * 100).quantize(Decimal("0.01")) if cost_of_goods > 0 else None
    )
    counts = StockCount.objects.filter(cutoff_at__gte=start)
    return {
        "days": days,
        "loss_units": loss_units,
        "loss_value": loss_value,
        "gain_units": gain_units,
        "gain_value": gain_value,
        "net_value": gain_value - loss_value,
        "written_off_value": written_off_value,
        "cost_of_goods_dispensed": cost_of_goods,
        "as_percent_of_cogs": as_percent_of_cogs,
        "worst_items": sorted(by_item.values(), key=lambda row: row["value"], reverse=True)[:8],
        "counts_taken": counts.count(),
        "counts_approved": counts.filter(status=StockCount.Status.APPROVED).count(),
        "measured": counts.filter(status=StockCount.Status.APPROVED).exists(),
    }


def supplier_price_history(days=365, limit=12):
    """What the hospital has paid per unit, and how that has moved.

    A supplier drifting a price upward is leakage that no single delivery looks
    odd enough to catch. Comparing each delivered cost with the one before it
    makes the drift visible.
    """
    start = timezone.now() - timedelta(days=days)
    lines = (
        GoodsReceiptLine.objects
        .filter(receipt__delivered_at__gte=start)
        .select_related("receipt__purchase_order__supplier", "order_line__item")
        .order_by("order_line__item__name", "receipt__delivered_at")
    )
    history = {}
    for line in lines:
        item = line.order_line.item
        bucket = history.setdefault(item.pk, {"item": item, "entries": []})
        bucket["entries"].append({
            "delivered_at": line.receipt.delivered_at,
            "supplier": line.receipt.purchase_order.supplier.name,
            "unit_cost": line.actual_unit_cost,
            "quantity": line.quantity_received,
            "receipt": line.receipt,
        })

    rows = []
    for bucket in history.values():
        entries = bucket["entries"]
        latest = entries[-1]
        previous = entries[-2] if len(entries) > 1 else None
        change = None
        percent = None
        if previous and previous["unit_cost"] > 0:
            change = (latest["unit_cost"] - previous["unit_cost"]).quantize(Decimal("0.01"))
            percent = (change / previous["unit_cost"] * 100).quantize(Decimal("0.01"))
        costs = [entry["unit_cost"] for entry in entries]
        rows.append({
            "item": bucket["item"],
            "entries": entries,
            "delivery_count": len(entries),
            "latest": latest,
            "previous": previous,
            "change": change,
            "percent_change": percent,
            "lowest": min(costs),
            "highest": max(costs),
        })
    rows.sort(key=lambda row: abs(row["percent_change"] or Decimal("0")), reverse=True)
    return {
        "days": days,
        "rows": rows[:limit],
        "rising": [row for row in rows if row["percent_change"] and row["percent_change"] > 0],
    }


def control_adoption(days=30):
    """Whether the controls are actually being used, or quietly bypassed.

    A control nobody follows looks identical to a control nobody needed. These
    are the numbers that tell the two apart.
    """
    start = timezone.now() - timedelta(days=days)
    receipts = GoodsReceipt.objects.filter(delivered_at__gte=start)
    total = receipts.count()
    with_photo = receipts.exclude(invoice_photo="").count()
    checked = receipts.filter(checked_by__isnull=False).count()

    prompt_cutoff = timedelta(hours=24)
    checked_promptly = sum(
        1 for receipt in receipts.filter(checked_by__isnull=False).only("checked_at", "delivered_at")
        if receipt.checked_at and (receipt.checked_at - receipt.delivered_at) <= prompt_cutoff
    )
    counts = StockCount.objects.filter(cutoff_at__gte=start)
    issues = DepartmentIssue.objects.filter(issued_at__gte=start)

    def percent(part, whole):
        return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1")) if whole else None

    return {
        "days": days,
        "deliveries": total,
        "deliveries_with_evidence": with_photo,
        "evidence_percent": percent(with_photo, total),
        "deliveries_checked": checked,
        "checked_percent": percent(checked, total),
        "checked_within_24h": checked_promptly,
        "checked_promptly_percent": percent(checked_promptly, total),
        "counts_taken": counts.count(),
        "counts_approved": counts.filter(status=StockCount.Status.APPROVED).count(),
        "issues_made": issues.count(),
        "issues_outstanding": issues.filter(status=DepartmentIssue.Status.OUTSTANDING).count(),
        "write_offs_pending": StockWriteOff.objects.filter(status=StockWriteOff.Status.PENDING).count(),
    }
