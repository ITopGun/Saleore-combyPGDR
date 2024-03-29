# Generated by Django 3.2.13 on 2022-04-29 08:51

from django.db import migrations
from django.db.models import F, OuterRef, Subquery
from django.contrib.postgres.functions import RandomUUID
from django.contrib.postgres.operations import CryptoExtension


def set_checkout_line_token_and_created_at(apps, _schema_editor):
    CheckoutLine = apps.get_model("checkout", "CheckoutLine")
    Checkout = apps.get_model("checkout", "Checkout")

    CheckoutLine.objects.filter(token__isnull=True).update(
        token=RandomUUID(),
        created_at=Subquery(
            Checkout.objects.filter(lines=OuterRef("id")).values("created_at")[:1]
        ),
    )


def set_checkout_line_old_id(apps, schema_editor):
    CheckoutLine = apps.get_model("checkout", "CheckoutLine")
    CheckoutLine.objects.all().update(old_id=F("id"))


class Migration(migrations.Migration):
    dependencies = [
        ("checkout", "0043_add_token_old_id_created_at_to_checkout_line"),
    ]

    operations = [
        CryptoExtension(),
        migrations.RunPython(
            set_checkout_line_token_and_created_at,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            set_checkout_line_old_id, reverse_code=migrations.RunPython.noop
        ),
    ]
