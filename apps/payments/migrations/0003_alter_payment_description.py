from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0002_payment_payment_amount_cannot_be_negative_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="description",
            field=models.TextField(blank=True, verbose_name="descrição"),
        ),
    ]
