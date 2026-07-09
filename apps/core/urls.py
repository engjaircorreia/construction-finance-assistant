from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path

from . import views


def healthcheck(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", lambda _request: redirect("internal_dashboard"), name="home"),
    path("health/", healthcheck, name="healthcheck"),
    path("interno/dashboard/", views.dashboard, name="internal_dashboard"),
    path("interno/fechamento/", views.monthly_closing, name="internal_monthly_closing"),
    path("interno/diagnostico/", views.operational_diagnostics, name="internal_operational_diagnostics"),
    path("interno/cadastros/counterparties/novo/", views.counterparty_create, name="internal_counterparty_create"),
    path("interno/cadastros/vendors/novo/", views.supplier_quick_create, name="internal_supplier_quick_create"),
    path("interno/cadastros/workers/novo/", views.worker_quick_create, name="internal_worker_quick_create"),
    path(
        "interno/cadastros/projects/novo/",
        views.work_cost_center_quick_create,
        name="internal_work_cost_center_quick_create",
    ),
    path(
        "interno/projects/<int:pk>/orcamento/importar/",
        views.work_budget_import,
        name="internal_work_budget_import",
    ),
    path("interno/payments/", views.pending_payments, name="internal_pending_payments"),
    path("interno/payments/acao-em-lote/", views.payment_bulk_action, name="internal_payment_bulk_action"),
    path("interno/drafts/", views.telegram_drafts, name="internal_telegram_drafts"),
    path("interno/drafts/<int:pk>/", views.telegram_draft_detail, name="internal_telegram_draft_detail"),
    path("interno/drafts/<int:pk>/editar/", views.telegram_draft_update, name="internal_telegram_draft_update"),
    path(
        "interno/drafts/<int:pk>/finalizar/",
        views.telegram_draft_action,
        {"action": "finalize"},
        name="internal_telegram_draft_finalize",
    ),
    path(
        "interno/drafts/<int:pk>/cancelar/",
        views.telegram_draft_action,
        {"action": "cancel"},
        name="internal_telegram_draft_cancel",
    ),
    path("interno/drafts/<int:pk>/<str:action>/", views.telegram_draft_action, name="internal_telegram_draft_action"),
    path("interno/payments/novo/", views.payment_create, name="internal_payment_create"),
    path("interno/payments/<int:pk>/editar/", views.payment_update, name="internal_payment_update"),
    path("interno/payments/<int:pk>/excluir/", views.payment_delete, name="internal_payment_delete"),
    path("interno/payments/<int:pk>/", views.payment_detail, name="internal_payment_detail"),
    path("interno/payments/<int:pk>/<str:action>/", views.payment_action, name="internal_payment_action"),
    path("interno/ofx/pendings/", views.unreconciled_ofx_transactions, name="internal_unreconciled_ofx"),
    path("interno/ofx/zerar-periodo/", views.clear_ofx_period, name="internal_ofx_clear_period"),
    path("interno/ofx/payments/edicao-em-lote/", views.ofx_payment_bulk_edit, name="internal_ofx_payment_bulk_edit"),
    path("interno/ofx/transacoes/<int:pk>/<str:action>/", views.ofx_transaction_action, name="internal_ofx_action"),
    path("interno/exportacoes/", views.export_batches, name="internal_export_batches"),
    path("interno/exportacoes/<int:pk>/download/", views.export_download, name="internal_export_download"),
    path(
        "interno/exportacoes/<int:pk>/download/<str:file_kind>/",
        views.export_download,
        name="internal_export_download_kind",
    ),
]
