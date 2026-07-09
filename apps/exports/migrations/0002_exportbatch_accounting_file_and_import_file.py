from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("exports", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="exportbatch",
            name="accounting_template_path",
            field=models.CharField(blank=True, max_length=255, verbose_name="modelo de exportação usado"),
        ),
        migrations.AddField(
            model_name="exportbatch",
            name="import_template_path",
            field=models.CharField(blank=True, max_length=255, verbose_name="modelo de importação usado"),
        ),
        migrations.AddField(
            model_name="exportbatch",
            name="accounting_file",
            field=models.FileField(
                blank=True,
                upload_to="generated/payments/%Y/%m/",
                verbose_name="planilha de exportação para contador",
            ),
        ),
        migrations.AddField(
            model_name="exportbatch",
            name="import_file",
            field=models.FileField(
                blank=True,
                upload_to="generated/payments/%Y/%m/",
                verbose_name="planilha de importação para sistema",
            ),
        ),
    ]
