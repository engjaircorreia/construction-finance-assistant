from apps.payments.models import Payment


EXPORTABLE_PAYMENT_STATUSES = [Payment.Status.APPROVED, Payment.Status.RECONCILED]


def approved_payments_for_export():
    return Payment.objects.filter(status__in=EXPORTABLE_PAYMENT_STATUSES)


def approved_payments_for_export_period(period_start, period_end):
    return approved_payments_for_export().filter(payment_date__gte=period_start, payment_date__lte=period_end)
