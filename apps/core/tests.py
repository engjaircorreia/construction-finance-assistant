import logging
import os
import subprocess
import sys
from io import StringIO

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import Resolver404, resolve

from apps.core.log_safety import (
    SafeFormatter,
    SafeLogFilter,
    mask_document,
    mask_secret,
    sanitize_log_payload,
)


class ProjectConfigurationTests(SimpleTestCase):
    valid_production_secret = "test-production-secret-key-with-more-than-fifty-characters-1234567890"

    def test_project_root_points_to_current_project(self):
        self.assertEqual(settings.PROJECT_ROOT, settings.BASE_DIR)
        self.assertTrue((settings.PROJECT_ROOT / "manage.py").exists())
        self.assertTrue(settings.PAYMENT_IMPORT_TEMPLATE.exists())

    def test_admin_url_uses_configured_non_default_path(self):
        self.assertNotEqual(settings.ADMIN_URL, "admin/")
        match = resolve(f"/{settings.ADMIN_URL}")

        self.assertEqual(match.app_name, "admin")

    def test_default_admin_path_is_not_available(self):
        with self.assertRaises(Resolver404):
            resolve("/admin/")

    def test_gitignore_keeps_sensitive_runtime_files_out_of_version_control(self):
        gitignore = (settings.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        for pattern in (
            ".env",
            "/storage/*",
            "/uploads/",
            "/ofx/",
            "/generated/",
            "/private/",
            "/backups/",
            "/certbot/",
            "/letsencrypt/",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, gitignore)

    def test_dockerignore_keeps_sensitive_runtime_files_out_of_build_context(self):
        dockerignore = (settings.PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

        for pattern in (".env", ".env.*", "storage", "uploads", "ofx", "generated", "private", "backups", "certbot"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, dockerignore)

    def test_login_protection_uses_django_axes(self):
        self.assertIn("axes", settings.INSTALLED_APPS)
        self.assertIn("axes.middleware.AxesMiddleware", settings.MIDDLEWARE)
        self.assertIn("axes.backends.AxesStandaloneBackend", settings.AUTHENTICATION_BACKENDS)
        self.assertIsNotNone(settings.AXES_ENABLED)
        self.assertGreaterEqual(settings.AXES_FAILURE_LIMIT, 1)

    def test_production_deploy_files_exist_and_do_not_use_example_env(self):
        compose = settings.PROJECT_ROOT / "docker-compose.prod.yml"
        nginx_http = settings.PROJECT_ROOT / "deploy/nginx/conf.d/app.http.conf"
        nginx_https = settings.PROJECT_ROOT / "deploy/nginx/conf.d/app.https.conf.example"
        production_doc = settings.PROJECT_ROOT / "docs/production.md"

        self.assertTrue(compose.exists())
        self.assertTrue(nginx_http.exists())
        self.assertTrue(nginx_https.exists())
        self.assertTrue(production_doc.exists())
        self.assertNotIn(".env.example", compose.read_text(encoding="utf-8"))

    def test_shared_vps_compose_keeps_database_and_redis_private(self):
        compose = (settings.PROJECT_ROOT / "docker-compose.shared-vps.yml").read_text(encoding="utf-8")
        web_block = self._compose_service_block(compose, "web")
        db_block = self._compose_service_block(compose, "db")
        redis_block = self._compose_service_block(compose, "redis")

        self.assertIn('"127.0.0.1:${APP_HOST_PORT:-8010}:8000"', web_block)
        self.assertNotIn("\n    ports:", db_block)
        self.assertNotIn("\n    ports:", redis_block)
        self.assertIn("postgres_data:/var/lib/postgresql/data", db_block)
        self.assertIn("redis_data:/data", redis_block)

    def test_shared_vps_compose_uses_persistent_storage_volumes(self):
        compose = (settings.PROJECT_ROOT / "docker-compose.shared-vps.yml").read_text(encoding="utf-8")

        for service in ("web", "worker", "beat", "bot"):
            with self.subTest(service=service):
                self.assertIn("app_storage:/app/storage", self._compose_service_block(compose, service))
        self.assertIn("postgres_data:", compose)
        self.assertIn("redis_data:", compose)
        self.assertIn("staticfiles:", compose)

    def test_backup_script_and_restore_documentation_exist(self):
        backup_script = settings.PROJECT_ROOT / "deploy/backup.sh"
        restore_doc = settings.PROJECT_ROOT / "docs/backup_restore.md"

        self.assertTrue(backup_script.exists())
        self.assertTrue(os.access(backup_script, os.X_OK))
        self.assertTrue(restore_doc.exists())

    def test_backup_script_uses_external_backup_root_and_retention(self):
        backup_script = (settings.PROJECT_ROOT / "deploy/backup.sh").read_text(encoding="utf-8")

        self.assertIn("BACKUP_ROOT", backup_script)
        self.assertIn("/backups/construction_finance_assistant", backup_script)
        self.assertIn("DAILY_RETENTION_DAYS", backup_script)
        self.assertIn("MONTHLY_RETENTION_DAYS", backup_script)
        self.assertIn("pg_dump", backup_script)
        self.assertIn("-C /app storage", backup_script)
        self.assertIn("BACKUP_ROOT deve ficar fora do repositorio", backup_script)

    def test_backup_script_does_not_reference_sensitive_secret_values(self):
        backup_script = (settings.PROJECT_ROOT / "deploy/backup.sh").read_text(encoding="utf-8")

        for forbidden in (
            "POSTGRES_PASSWORD",
            "DATABASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_API_KEY",
            "DJANGO_SECRET_KEY",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, backup_script)

    def test_backup_restore_documentation_describes_manual_backup_and_restore(self):
        restore_doc = (settings.PROJECT_ROOT / "docs/backup_restore.md").read_text(encoding="utf-8")

        for expected in (
            "deploy/backup.sh",
            "/backups/construction_finance_assistant",
            "cron",
            "Restore In A Controlled Environment",
            "Local Restore Simulation",
            "COMPOSE_PROJECT_NAME=construction_finance_restore_test",
            "pg_dump",
            "psql",
            "storage",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, restore_doc)

    def test_backup_paths_are_ignored_by_git_and_docker(self):
        gitignore = (settings.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (settings.PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("/backups/", gitignore)
        self.assertIn("backups", dockerignore)

    def test_nginx_does_not_publish_private_media_files(self):
        for relative_path in (
            "deploy/nginx/conf.d/app.http.conf",
            "deploy/nginx/conf.d/app.https.conf.example",
        ):
            with self.subTest(path=relative_path):
                nginx_config = (settings.PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("location /media/", nginx_config)
                self.assertNotIn("alias /app/storage/media/", nginx_config)

    def test_production_settings_reject_unsafe_secret_key(self):
        result = self._import_production_settings(
            DJANGO_SECRET_KEY="unsafe-local-development-key",
            DJANGO_ALLOWED_HOSTS="sistema.example.com",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://sistema.example.com",
            DJANGO_ADMIN_URL="painel-reservado/",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr + result.stdout)

    def test_production_settings_reject_empty_or_short_secret_key(self):
        for secret in ("", "short-secret"):
            with self.subTest(secret=secret):
                result = self._import_production_settings(
                    DJANGO_SECRET_KEY=secret,
                    DJANGO_ALLOWED_HOSTS="sistema.example.com",
                    DJANGO_CSRF_TRUSTED_ORIGINS="https://sistema.example.com",
                    DJANGO_ADMIN_URL="painel-reservado/",
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DJANGO_SECRET_KEY", result.stderr + result.stdout)

    def test_production_settings_reject_open_or_local_allowed_hosts(self):
        for host in ("*", ".example.com", "localhost", "127.0.0.1", "0.0.0.0"):
            with self.subTest(host=host):
                result = self._import_production_settings(
                    DJANGO_SECRET_KEY=self.valid_production_secret,
                    DJANGO_ALLOWED_HOSTS=host,
                    DJANGO_CSRF_TRUSTED_ORIGINS="https://sistema.example.com",
                    DJANGO_ADMIN_URL="painel-reservado/",
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DJANGO_ALLOWED_HOSTS", result.stderr + result.stdout)

    def test_production_settings_reject_missing_csrf_trusted_origins(self):
        result = self._import_production_settings(
            DJANGO_SECRET_KEY=self.valid_production_secret,
            DJANGO_ALLOWED_HOSTS="sistema.example.com",
            DJANGO_CSRF_TRUSTED_ORIGINS="",
            DJANGO_ADMIN_URL="painel-reservado/",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_CSRF_TRUSTED_ORIGINS", result.stderr + result.stdout)

    def test_production_settings_reject_insecure_or_mismatched_csrf_origins(self):
        for origin in ("http://sistema.example.com", "https://*.example.com", "https://outro.example.com"):
            with self.subTest(origin=origin):
                result = self._import_production_settings(
                    DJANGO_SECRET_KEY=self.valid_production_secret,
                    DJANGO_ALLOWED_HOSTS="sistema.example.com",
                    DJANGO_CSRF_TRUSTED_ORIGINS=origin,
                    DJANGO_ADMIN_URL="painel-reservado/",
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DJANGO_CSRF_TRUSTED_ORIGINS", result.stderr + result.stdout)

    def test_production_settings_reject_default_admin_url(self):
        result = self._import_production_settings(
            DJANGO_SECRET_KEY=self.valid_production_secret,
            DJANGO_ALLOWED_HOSTS="sistema.example.com",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://sistema.example.com",
            DJANGO_ADMIN_URL="controle-interno/",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ADMIN_URL", result.stderr + result.stdout)

    def test_production_settings_enable_https_and_secure_cookies(self):
        result = self._run_production_settings_script(
            "import config.settings.production as s; "
            "print(s.DEBUG, s.SECURE_SSL_REDIRECT, s.SESSION_COOKIE_SECURE, "
            "s.CSRF_COOKIE_SECURE, s.SECURE_PROXY_SSL_HEADER, "
            "s.SECURE_HSTS_SECONDS, s.SECURE_HSTS_PRELOAD)"
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("False True True True ('HTTP_X_FORWARDED_PROTO', 'https')", result.stdout)
        self.assertIn("2592000 True", result.stdout)

    def _import_production_settings(self, **env_overrides):
        return self._run_production_settings_script("import config.settings.production", **env_overrides)

    def _run_production_settings_script(self, code, **env_overrides):
        env = os.environ.copy()
        env.update(
            {
                "DJANGO_SECRET_KEY": self.valid_production_secret,
                "DJANGO_ALLOWED_HOSTS": "sistema.example.com",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://sistema.example.com",
                "DJANGO_ADMIN_URL": "painel-reservado/",
                "DJANGO_DEBUG": "False",
            }
        )
        env.update(env_overrides)
        env["DJANGO_SETTINGS_MODULE"] = "config.settings.production"

        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=settings.PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _compose_service_block(self, compose_text, service_name):
        lines = compose_text.splitlines()
        start_marker = f"  {service_name}:"
        start = lines.index(start_marker)
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
                end = index
                break
        return "\n".join(lines[start:end])


class LogSafetyTests(SimpleTestCase):
    def test_mask_document_masks_cpf(self):
        masked = mask_document("CPF do vendor 123.456.789-01")

        self.assertIn("***.***.***-**", masked)
        self.assertNotIn("123.456.789-01", masked)

    def test_mask_document_masks_cnpj(self):
        masked = mask_document("CNPJ 12.345.678/0001-99")

        self.assertIn("**.***.***/****-**", masked)
        self.assertNotIn("12.345.678/0001-99", masked)

    def test_mask_secret_never_reveals_value(self):
        self.assertEqual(mask_secret("sk-secret-value"), "[removed]")

    def test_logging_configuration_uses_safe_filter_and_formatter(self):
        self.assertEqual(
            settings.LOGGING["filters"]["sanitize_sensitive"]["()"],
            "apps.core.log_safety.SafeLogFilter",
        )
        self.assertEqual(
            settings.LOGGING["formatters"]["safe"]["()"],
            "apps.core.log_safety.SafeFormatter",
        )
        self.assertIn("sanitize_sensitive", settings.LOGGING["handlers"]["console"]["filters"])

    @override_settings(
        OPENAI_API_KEY="sk-openai-secret-for-log-test",
        TELEGRAM_BOT_TOKEN="123456:telegram-secret-for-log-test",
        SECRET_KEY="django-secret-for-log-test",
    )
    def test_sanitize_log_payload_removes_tokens_and_keys(self):
        payload = {
            "telegram_bot_token": "123456:telegram-secret-for-log-test",
            "openai_api_key": "sk-openai-secret-for-log-test",
            "secret_key": "django-secret-for-log-test",
            "database_url": "postgres://user:super-secret-password@db:5432/app",
            "message": "CPF 12345678901 CNPJ 12345678000199 token=abc123",
            "content": b"conteudo sensivel do receipt",
        }

        sanitized = str(sanitize_log_payload(payload))

        self.assertNotIn("telegram-secret-for-log-test", sanitized)
        self.assertNotIn("sk-openai-secret-for-log-test", sanitized)
        self.assertNotIn("django-secret-for-log-test", sanitized)
        self.assertNotIn("super-secret-password", sanitized)
        self.assertNotIn("12345678901", sanitized)
        self.assertNotIn("12345678000199", sanitized)
        self.assertNotIn("conteudo sensivel do receipt", sanitized)
        self.assertIn("[removed]", sanitized)
        self.assertIn("***.***.***-**", sanitized)
        self.assertIn("**.***.***/****-**", sanitized)
        self.assertIn("<bytes", sanitized)

    @override_settings(TELEGRAM_BOT_TOKEN="123456:telegram-secret-for-log-test")
    def test_telegram_logs_do_not_expose_token(self):
        output = self.safe_log_output(
            "apps.telegrambot.services",
            "Telegram failure token=%s",
            settings.TELEGRAM_BOT_TOKEN,
        )

        self.assertNotIn("telegram-secret-for-log-test", output)
        self.assertIn("[removed]", output)

    def safe_log_output(self, logger_name, message, *args, exc_info=None):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SafeLogFilter())
        handler.setFormatter(SafeFormatter("%(levelname)s %(name)s %(message)s"))
        logger = logging.getLogger(logger_name)
        old_handlers = logger.handlers[:]
        old_level = logger.level
        old_propagate = logger.propagate
        try:
            logger.handlers = [handler]
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.error(message, *args, exc_info=exc_info)
        finally:
            logger.handlers = old_handlers
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        return stream.getvalue()
