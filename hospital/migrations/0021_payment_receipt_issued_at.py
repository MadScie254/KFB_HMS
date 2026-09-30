from django.db import migrations, models
from django.db.models import F


def mark_existing_receipts_as_issued(apps, schema_editor):
    Payment = apps.get_model("hospital", "Payment")
    # Existing payment pages may already have been printed. Treat those as
    # issued so an old direct URL never produces an unmarked original again.
    Payment.objects.filter(receipt_issued_at__isnull=True).update(receipt_issued_at=F("received_at"))


class Migration(migrations.Migration):
    dependencies = [("hospital", "0020_supplierchangerequest")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="receipt_issued_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_receipts_as_issued, migrations.RunPython.noop),
    ]
