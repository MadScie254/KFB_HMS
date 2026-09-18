from django.db import migrations


def protect_audit_events(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        CREATE OR REPLACE FUNCTION kfb_protect_audit_event()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'hospital_auditevent is append-only';
        END;
        $$ LANGUAGE plpgsql;
    """)
    schema_editor.execute("""
        DROP TRIGGER IF EXISTS kfb_audit_event_append_only ON hospital_auditevent;
        CREATE TRIGGER kfb_audit_event_append_only
        BEFORE UPDATE OR DELETE ON hospital_auditevent
        FOR EACH ROW EXECUTE FUNCTION kfb_protect_audit_event();
    """)


def remove_audit_protection(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS kfb_audit_event_append_only ON hospital_auditevent;")
    schema_editor.execute("DROP FUNCTION IF EXISTS kfb_protect_audit_event();")


class Migration(migrations.Migration):
    dependencies = [("hospital", "0003_loginattempt_patient_hospital_pa_id_numb_8779d4_idx_and_more")]
    operations = [migrations.RunPython(protect_audit_events, remove_audit_protection)]
