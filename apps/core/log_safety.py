from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence

from django.conf import settings


SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "celery_broker_url",
    "celery_result_backend",
    "database_url",
    "db_password",
    "django_secret_key",
    "openai_api_key",
    "password",
    "postgres_password",
    "secret",
    "secret_key",
    "senha",
    "telegram_bot_token",
    "token",
}
SENSITIVE_SETTING_NAMES = {
    "SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "DB_PASSWORD",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
}
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|senha|api[_-]?key|authorization)\s*[:=]\s*['\"]?[^'\"\s,;]+"
)
URL_PASSWORD_RE = re.compile(r"(?P<prefix>\b[\w+.-]+://[^:\s/@]+):[^@\s]+@")
CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
CPF_RE = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")


def mask_document(value) -> str:
    text = str(value or "")
    text = CNPJ_RE.sub("**.***.***/****-**", text)
    return CPF_RE.sub("***.***.***-**", text)


def mask_secret(value) -> str:
    return "[removed]" if value else ""


def sanitize_log_text(value, *, limit: int | None = 1200) -> str:
    text = str(value or "")
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[removed]", text)
    text = URL_PASSWORD_RE.sub(r"\g<prefix>:[removed]@", text)
    for secret in sensitive_values():
        text = text.replace(secret, "[removed]")
    text = mask_document(text)
    text = " ".join(text.split())
    if limit and len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def sanitize_log_payload(payload, *, max_string_length: int = 400, max_items: int = 20):
    if isinstance(payload, Mapping):
        sanitized = {}
        for index, (key, value) in enumerate(payload.items()):
            if index >= max_items:
                sanitized["..."] = "[truncado]"
                break
            key_text = str(key)
            if is_sensitive_key(key_text):
                sanitized[key] = "[removed]"
            else:
                sanitized[key] = sanitize_log_payload(
                    value,
                    max_string_length=max_string_length,
                    max_items=max_items,
                )
        return sanitized
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return f"<bytes {len(payload)}>"
    if isinstance(payload, str):
        return sanitize_log_text(payload, limit=max_string_length)
    if isinstance(payload, Sequence) and not isinstance(payload, str):
        values = list(payload[:max_items])
        sanitized_values = [
            sanitize_log_payload(value, max_string_length=max_string_length, max_items=max_items) for value in values
        ]
        if len(payload) > max_items:
            sanitized_values.append("[truncado]")
        return type(payload)(sanitized_values) if isinstance(payload, tuple) else sanitized_values
    return payload


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in SENSITIVE_KEYS or any(part in SENSITIVE_KEYS for part in normalized.split("_"))


def sensitive_values() -> list[str]:
    values = []
    for name in SENSITIVE_SETTING_NAMES:
        value = getattr(settings, name, "")
        if isinstance(value, str) and len(value) >= 8:
            values.append(value)
        env_value = os.getenv(name)
        if isinstance(env_value, str) and len(env_value) >= 8:
            values.append(env_value)
    try:
        database_password = settings.DATABASES.get("default", {}).get("PASSWORD", "")
    except Exception:
        database_password = ""
    if isinstance(database_password, str) and len(database_password) >= 8:
        values.append(database_password)
    return sorted(set(values), key=len, reverse=True)


class SafeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args:
            record.msg = sanitize_log_payload(record.msg)
        if isinstance(record.args, Mapping):
            record.args = sanitize_log_payload(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(sanitize_log_payload(arg) for arg in record.args)
        elif record.args:
            record.args = sanitize_log_payload(record.args)
        return True


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(super().format(record), limit=None)

    def formatException(self, exc_info) -> str:
        return sanitize_log_text(super().formatException(exc_info), limit=None)
