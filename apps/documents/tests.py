from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import UploadedFile


class UploadedFileTests(TestCase):
    def test_uploaded_file_is_admin_registered(self):
        self.assertTrue(admin.site.is_registered(UploadedFile))

    def test_uploaded_file_defaults_to_manual_other_and_received(self):
        uploaded = UploadedFile.objects.create(original_filename="receipt.pdf")

        self.assertEqual(uploaded.source, UploadedFile.Source.MANUAL)
        self.assertEqual(uploaded.kind, UploadedFile.Kind.OTHER)
        self.assertEqual(uploaded.status, UploadedFile.Status.RECEIVED)

    def test_invalid_uploaded_file_status_is_rejected_by_model_validation(self):
        uploaded = UploadedFile(original_filename="receipt.pdf", status="status_inexistente")

        with self.assertRaises(ValidationError):
            uploaded.full_clean()
