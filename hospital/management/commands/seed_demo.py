from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from hospital.models import (
    Bed,
    CatalogueItem,
    Encounter,
    ExceptionRecord,
    EyeCase,
    EyeSession,
    Patient,
    PriceVersion,
    Role,
    Setting,
    StaffProfile,
    StockBatch,
    StockMovement,
    Ward,
)
from hospital.services import prepare_pharmacy_order

DEMO_PASSWORD = "Demo-Only-2026!"


class Command(BaseCommand):
    help = "Seed fictional demonstration data. Refuses to run outside KFB_ENV=demo."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError("Demo seeding is disabled outside KFB_ENV=demo.")

        users = {}
        for username, role, name in [
            ("owner.demo", Role.OWNER, "Grace Wekesa (Demo)"),
            ("reception.demo", Role.RECEPTION, "Mercy Naliaka (Demo)"),
            ("clinician.demo", Role.CLINICIAN, "Dr. Daniel Otieno (Demo)"),
            ("nurse.demo", Role.NURSE, "Faith Nasimiyu (Demo)"),
            ("pharmacy.demo", Role.PHARMACY, "Peter Wafula (Demo)"),
            ("lab.demo", Role.LAB, "Anne Barasa (Demo)"),
            ("eye.demo", Role.EYE, "Lucy Nekesa (Demo)"),
            ("procurement.demo", Role.PROCUREMENT, "Samuel Khaemba (Demo)"),
            ("reviewer.demo", Role.REVIEWER, "Ruth Simiyu (Demo)"),
        ]:
            user, created = User.objects.get_or_create(username=username, defaults={"first_name": name.split()[0], "last_name": " ".join(name.split()[1:])})
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save(update_fields=["password"])
            profile, _ = StaffProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.display_name = name
            profile.active_shift_label = "Day shift" if role in {Role.RECEPTION, Role.NURSE} else "On duty"
            profile.save()
            users[role] = user

        setup = [
            ("official_receipt_header", "Kingdom Faith Based Hospital · Malaha, Webuye", "Verify postal, contact and fiscal wording before live use.", False),
            ("delegated_approver", "Role configured; named production user not appointed", "Sensitive changes remain disabled until an accountable reviewer is appointed.", False),
            ("eye_package_right", "KES 12,000", "Provisional per-eye price from owner briefing; version before production use.", False),
            ("eye_package_left", "KES 12,000", "Provisional per-eye price from owner briefing; version before production use.", False),
            ("eye_doctor_case_fee", "KES 2,000 per completed patient", "Confirm bilateral and repeated-session treatment before live payout.", False),
            ("eye_package_inclusions", "Not configured", "Qualified staff must list included medicines, tests and services.", False),
            ("bed_charging_rule", "Not configured", "Set start, end and rounding rules; no automatic accrual yet.", False),
            ("bed_register", "Demo has 6 fictional beds", "Replace with witnessed list of actual usable beds.", False),
            ("mpesa_integration", "Manual recording — unverified", "Live callback requires verified account capability, credentials and reachable infrastructure.", False),
            ("remote_owner_access", "Not configured", "Use a private authenticated network path; never expose the database port.", False),
            ("tax_and_fiscal_receipts", "Not verified", "Confirm current Kenyan fiscal obligations with an authoritative adviser.", False),
            ("backup_encryption_recipient", "Not configured", "Owner must hold tested recovery keys and separate backup media.", False),
            ("clinical_templates", "Demo fields only", "Qualified clinical staff must review templates before real care.", False),
        ]
        for key, value, description, confirmed in setup:
            Setting.objects.update_or_create(key=key, defaults={"value": value, "description": description, "production_confirmed": confirmed, "updated_by": users[Role.OWNER]})

        product_rows = [
            ("MED-PARA500", "Paracetamol 500 mg tablet", "Pharmacy", "tablet", Decimal("5.00"), Decimal("50"), False),
            ("MED-AMOX500", "Amoxicillin 500 mg capsule", "Pharmacy", "capsule", Decimal("18.00"), Decimal("40"), True),
            ("MED-ORS", "Oral rehydration salts sachet", "Pharmacy", "sachet", Decimal("35.00"), Decimal("20"), False),
        ]
        products = {}
        for code, name, department, unit, price, reorder, rx in product_rows:
            item, _ = CatalogueItem.objects.update_or_create(code=code, defaults={"name": name, "kind": CatalogueItem.Kind.PRODUCT, "department": department, "base_unit": unit, "sale_unit": unit, "units_per_sale_unit": 1, "reorder_level": reorder, "active": True, "prescription_required": rx})
            PriceVersion.objects.get_or_create(item=item, amount=price, defaults={"reason": "Fictional demo price", "approved_by": users[Role.OWNER]})
            products[code] = item

        for code, name, department, price in [
            ("SVC-CONSULT", "Outpatient consultation", "Outpatient", Decimal("500.00")),
            ("SVC-LAB-DEMO", "Laboratory service (demo)", "Laboratory", Decimal("750.00")),
            ("SVC-EYE-R", "Eye surgery package — right eye", "Eye Clinic", Decimal("12000.00")),
            ("SVC-EYE-B", "Eye surgery package — both eyes", "Eye Clinic", Decimal("24000.00")),
        ]:
            item, _ = CatalogueItem.objects.update_or_create(code=code, defaults={"name": name, "kind": CatalogueItem.Kind.SERVICE, "department": department, "base_unit": "service", "sale_unit": "service", "units_per_sale_unit": 1, "active": True, "provisional": code.startswith("SVC-EYE")})
            PriceVersion.objects.get_or_create(item=item, amount=price, defaults={"reason": "Fictional demo / provisional price", "approved_by": users[Role.OWNER]})

        for item, batch_no, expiry, qty, cost in [
            (products["MED-PARA500"], "DEMO-PARA-01", date.today() + timedelta(days=420), Decimal("200"), Decimal("2.10")),
            (products["MED-AMOX500"], "DEMO-AMOX-01", date.today() + timedelta(days=210), Decimal("80"), Decimal("9.50")),
            (products["MED-ORS"], "DEMO-ORS-01", date.today() + timedelta(days=32), Decimal("18"), Decimal("20.00")),
        ]:
            batch, _ = StockBatch.objects.get_or_create(item=item, batch_number=batch_no, defaults={"expiry_date": expiry, "purchase_cost_per_base_unit": cost})
            StockMovement.objects.get_or_create(
                idempotency_key=f"demo-opening:{batch.pk}",
                defaults={"batch": batch, "movement_type": StockMovement.MovementType.RECEIPT, "quantity_delta": qty, "to_location": "Pharmacy", "reference_type": "OpeningCount", "reference_id": "DEMO-WITNESSED-001", "reason": "Fictional witnessed opening count", "entered_by": users[Role.PHARMACY]},
            )

        if not products["MED-ORS"].pharmacyorderitem_set.filter(order__customer_name="Demo Walk-In Customer").exists():
            prepare_pharmacy_order(
                actor=users[Role.PHARMACY],
                customer_name="Demo Walk-In Customer",
                patient=None,
                items=[(products["MED-ORS"], Decimal("2"))],
            )

        patient_rows = [
            ("Amina", "Nanjala", date(1992, 5, 14), "F", "0700000001", "none"),
            ("John", "Wanyonyi", date(1985, 11, 3), "M", "0700000002", "unknown"),
            ("Miriam", "Achieng", date(1968, 8, 22), "F", "0700000003", "recorded"),
        ]
        patients = []
        for first, last, dob, sex, phone, allergy in patient_rows:
            patient, _ = Patient.objects.get_or_create(first_name=first, last_name=last, phone=phone, defaults={"date_of_birth": dob, "sex": sex, "allergy_status": allergy, "allergy_details": "Fictional demo allergy—verify clinically" if allergy == "recorded" else "", "registered_by": users[Role.RECEPTION], "is_demo": True})
            patients.append(patient)

        Encounter.objects.get_or_create(patient=patients[0], department="Outpatient", status=Encounter.Status.CLINICIAN, defaults={"urgency": "routine", "started_by": users[Role.RECEPTION], "assigned_clinician": users[Role.CLINICIAN]})
        Encounter.objects.get_or_create(patient=patients[1], department="Laboratory", status=Encounter.Status.TESTS, defaults={"urgency": "urgent", "started_by": users[Role.RECEPTION], "assigned_clinician": users[Role.CLINICIAN]})

        ward, _ = Ward.objects.get_or_create(name="Demo General Ward")
        for number in range(1, 7):
            Bed.objects.get_or_create(ward=ward, label=f"D-{number:02d}")

        session, _ = EyeSession.objects.get_or_create(session_date=date.today() + timedelta(days=21), visiting_doctor="Dr. Visiting Clinician (Demo)", theatre="Main theatre", defaults={"status": "proposed"})
        EyeCase.objects.get_or_create(patient=patients[2], proposed_procedure="Eye procedure — pending clinical specification", defaults={"session": session, "eye": EyeCase.Eye.BOTH, "readiness": "assessment", "package_price": Decimal("24000.00")})

        ExceptionRecord.objects.get_or_create(category="near_expiry", summary="Demo ORS batch approaches expiry", defaults={"severity": "warning", "evidence": "Fictional batch DEMO-ORS-01; review FEFO and demand."})
        ExceptionRecord.objects.get_or_create(category="production_setup", summary="Independent production reviewer not appointed", defaults={"severity": "urgent", "evidence": "Sensitive approvals remain unavailable until configuration."})

        self.stdout.write(self.style.SUCCESS("Fictional demo data seeded."))
        self.stdout.write("Demo accounts: owner.demo, reception.demo, clinician.demo, nurse.demo, pharmacy.demo, lab.demo, eye.demo, procurement.demo, reviewer.demo")
        self.stdout.write(f"Demo-only password for all seeded accounts: {DEMO_PASSWORD}")
