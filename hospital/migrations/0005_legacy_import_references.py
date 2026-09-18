from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("hospital", "0004_protect_audit_events_postgresql")]
    operations = [
        migrations.AddField(
            model_name="patient",
            name="external_reference",
            field=models.CharField(blank=True, db_index=True, max_length=80),
        ),
        migrations.AddField(
            model_name="invoice",
            name="external_reference",
            field=models.CharField(blank=True, db_index=True, max_length=100),
        ),
        migrations.AddField(
            model_name="invoice",
            name="original_invoice_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
