from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import AuthorizedTelegramUser


class AuthorizedTelegramUserTests(TestCase):
    def test_authorized_telegram_user_is_admin_registered(self):
        self.assertTrue(admin.site.is_registered(AuthorizedTelegramUser))

    def test_telegram_user_id_must_be_unique(self):
        AuthorizedTelegramUser.objects.create(
            telegram_user_id=123456,
            name="Partner 1",
            username="socio1",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorizedTelegramUser.objects.create(
                telegram_user_id=123456,
                name="Partner 2",
                username="socio2",
            )

    def test_one_django_user_can_have_only_one_authorized_telegram_user(self):
        user = get_user_model().objects.create_user(username="jair", password="senha-forte-local")
        AuthorizedTelegramUser.objects.create(telegram_user_id=111, name="Jair", user=user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorizedTelegramUser.objects.create(telegram_user_id=222, name="Jair duplicado", user=user)
