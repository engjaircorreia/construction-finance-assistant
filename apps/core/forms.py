from django import forms

from apps.counterparties.importers import normalize_text
from apps.counterparties.models import (
    Category,
    ChartOfAccount,
    CostCenter,
    Counterparty,
    CounterpartyDocument,
    Origin,
    Work,
)
from apps.payments.defaults import find_default_company_cost_center, find_default_work_cost_center
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft


class PaymentManualForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = [
            "competence_date",
            "due_date",
            "payment_date",
            "amount",
            "counterparty",
            "description",
            "document_number",
            "category",
            "chart_account",
            "payment_method",
            "payer",
            "bank_account",
            "cost_center",
            "work",
            "work_item_index",
        ]
        widgets = {
            "competence_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "due_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "payment_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counterparty"].queryset = Counterparty.objects.filter(is_active=True).order_by("name")
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["chart_account"].queryset = ChartOfAccount.objects.filter(is_active=True).order_by("name")
        self.fields["cost_center"].queryset = CostCenter.objects.filter(is_active=True).order_by("name")
        self.fields["work"].queryset = Work.objects.filter(is_active=True).order_by("name")
        for name in [
            "competence_date",
            "due_date",
            "payment_date",
            "amount",
            "counterparty",
            "description",
            "document_number",
            "category",
            "chart_account",
            "payment_method",
            "payer",
            "bank_account",
            "cost_center",
            "work",
            "work_item_index",
        ]:
            self.fields[name].required = False
        self.fields["payment_date"].required = True
        self.fields["amount"].required = True
        self.fields["counterparty"].required = True

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        payment_date = cleaned.get("payment_date")
        counterparty = cleaned.get("counterparty")
        work = cleaned.get("work")

        if payment_date:
            cleaned["competence_date"] = cleaned.get("competence_date") or payment_date
            cleaned["due_date"] = cleaned.get("due_date") or payment_date

        if counterparty:
            cleaned["category"] = cleaned.get("category") or counterparty.default_category
            cleaned["chart_account"] = cleaned.get("chart_account") or counterparty.default_chart_account
            cleaned["cost_center"] = cleaned.get("cost_center") or counterparty.default_cost_center
            cleaned["work"] = work or counterparty.default_work
            work = cleaned.get("work")

        if not cleaned.get("cost_center"):
            cleaned["cost_center"] = find_default_work_cost_center() if work else find_default_company_cost_center()

        if not cleaned.get("category"):
            self.add_error("category", "Enter a category or set a default category on the counterparty.")
        if not cleaned.get("cost_center"):
            self.add_error("cost_center", "Enter a cost center or create the Company/Project defaults.")
        if not cleaned.get("work"):
            cleaned["work_item_index"] = ""

        return cleaned

    def save(self, commit=True):
        payment = super().save(commit=False)
        is_new = payment.pk is None
        if is_new:
            payment.source = Origin.MANUAL
        payment.status = Payment.Status.PENDING_CONFIRMATION
        payment.needs_review = True
        payment.review_reason = "Manual payment saved in the web interface. Awaiting approval."
        payload = payment.raw_payload or {}
        payload["manual_web_entry" if is_new else "manual_web_update"] = True
        payment.raw_payload = payload
        if commit:
            payment.save()
            self.save_m2m()
        return payment


class TelegramDraftForm(forms.ModelForm):
    class Meta:
        model = TelegramDraft
        fields = [
            "payment_date",
            "amount",
            "counterparty",
            "description",
            "category",
            "payment_method",
            "cost_center",
            "work",
            "work_item_index",
        ]
        widgets = {
            "payment_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counterparty"].queryset = Counterparty.objects.filter(is_active=True).order_by("name")
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["cost_center"].queryset = CostCenter.objects.filter(is_active=True).order_by("name")
        self.fields["work"].queryset = Work.objects.filter(is_active=True).order_by("name")
        for name in self.fields:
            self.fields[name].required = False

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        counterparty = cleaned.get("counterparty")
        work = cleaned.get("work")
        if counterparty:
            cleaned["category"] = cleaned.get("category") or counterparty.default_category
            cleaned["cost_center"] = cleaned.get("cost_center") or counterparty.default_cost_center
            cleaned["work"] = work or counterparty.default_work
            work = cleaned.get("work")
        if not cleaned.get("cost_center"):
            cleaned["cost_center"] = find_default_work_cost_center() if work else find_default_company_cost_center()
        if not cleaned.get("work"):
            cleaned["work_item_index"] = ""
        return cleaned


class CounterpartyManualForm(forms.ModelForm):
    primary_document = forms.CharField(
        label="Primary taxpayer ID",
        required=False,
        max_length=32,
    )

    class Meta:
        model = Counterparty
        fields = [
            "name",
            "kind",
            "person_type",
            "primary_document",
            "default_category",
            "default_chart_account",
            "default_cost_center",
            "default_work",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["default_chart_account"].queryset = ChartOfAccount.objects.filter(is_active=True).order_by("name")
        self.fields["default_cost_center"].queryset = CostCenter.objects.filter(is_active=True).order_by("name")
        self.fields["default_work"].queryset = Work.objects.filter(is_active=True).order_by("name")
        self.fields["name"].required = True
        for name in [
            "primary_document",
            "default_category",
            "default_chart_account",
            "default_cost_center",
            "default_work",
            "notes",
        ]:
            self.fields[name].required = False

    def clean_primary_document(self):
        document = only_digits(self.cleaned_data.get("primary_document", ""))
        if document and len(document) not in {11, 14}:
            raise forms.ValidationError("Enter an 11-digit CPF or a 14-digit CNPJ.")
        if document and Counterparty.objects.filter(primary_document=document).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A vendor/worker is already registered with this CPF/CNPJ.")
        if (
            document
            and CounterpartyDocument.objects.filter(number=document)
            .exclude(counterparty_id=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("A document is already registered with this CPF/CNPJ.")
        return document

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name") or ""
        document = cleaned.get("primary_document") or ""
        normalized_name = normalize_text(name)
        if normalized_name and not document:
            duplicate = Counterparty.objects.filter(normalized_name=normalized_name).exclude(pk=self.instance.pk).first()
            if duplicate:
                self.add_error("name", f"A similar record already exists: {duplicate.name}.")
        cleaned["normalized_name"] = normalized_name
        return cleaned

    def save(self, commit=True):
        counterparty = super().save(commit=False)
        counterparty.normalized_name = self.cleaned_data["normalized_name"]
        counterparty.source = Origin.MANUAL
        counterparty.confidence = 1
        if counterparty.primary_document and counterparty.person_type == Counterparty.PersonType.UNKNOWN:
            counterparty.person_type = (
                Counterparty.PersonType.INDIVIDUAL
                if len(counterparty.primary_document) == 11
                else Counterparty.PersonType.COMPANY
            )
        if commit:
            counterparty.save()
            self.save_m2m()
            sync_primary_document(counterparty)
        return counterparty


def only_digits(value: str) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def sync_primary_document(counterparty: Counterparty) -> None:
    if not counterparty.primary_document:
        return
    document_type = (
        CounterpartyDocument.DocumentType.CPF
        if len(counterparty.primary_document) == 11
        else CounterpartyDocument.DocumentType.CNPJ
    )
    CounterpartyDocument.objects.update_or_create(
        number=counterparty.primary_document,
        defaults={
            "counterparty": counterparty,
            "document_type": document_type,
            "source": Origin.MANUAL,
            "confidence": 1,
            "is_primary": True,
        },
    )


class CounterpartyQuickForm(forms.ModelForm):
    primary_document = forms.CharField(
        label="CPF/CNPJ",
        required=False,
        max_length=32,
    )

    class Meta:
        model = Counterparty
        fields = [
            "name",
            "primary_document",
            "default_category",
        ]

    def __init__(self, *args, kind: str, reuse_existing: bool = False, **kwargs):
        self.kind = kind
        self.reuse_existing = reuse_existing
        self.existing_counterparty = None
        super().__init__(*args, **kwargs)
        self.fields["default_category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["default_category"].required = False
        self.fields["name"].label = "Name"
        self.fields["default_category"].label = "Default category"

    def counterparties_for_document(self, document: str) -> list[Counterparty]:
        matches = {counterparty.pk: counterparty for counterparty in Counterparty.objects.filter(primary_document=document)}
        for document_row in CounterpartyDocument.objects.select_related("counterparty").filter(number=document):
            matches[document_row.counterparty_id] = document_row.counterparty
        return list(matches.values())

    def clean_primary_document(self):
        document = only_digits(self.cleaned_data.get("primary_document", ""))
        if document and len(document) not in {11, 14}:
            raise forms.ValidationError("Enter an 11-digit CPF or a 14-digit CNPJ.")
        if document:
            matches = self.counterparties_for_document(document)
            if len(matches) == 1 and self.reuse_existing:
                self.existing_counterparty = matches[0]
            elif len(matches) > 1:
                raise forms.ValidationError(
                    "Ambiguous record: this CPF/CNPJ appears in more than one record. Review before linking."
                )
            elif matches:
                raise forms.ValidationError("A vendor/worker is already registered with this CPF/CNPJ.")
        return document

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name") or ""
        document = cleaned.get("primary_document") or ""
        normalized_name = normalize_text(name)
        if normalized_name and not self.existing_counterparty:
            matches = list(Counterparty.objects.filter(normalized_name=normalized_name).order_by("id")[:3])
            if matches and self.reuse_existing:
                if len(matches) == 1:
                    match = matches[0]
                    if document and match.primary_document and match.primary_document != document:
                        self.add_error(
                            "name",
                            f"A record with a similar name was found, but with a different CPF/CNPJ: {match.name}.",
                        )
                    else:
                        self.existing_counterparty = match
                else:
                    self.add_error(
                        "name",
                        "Ambiguous record: more than one similar name exists. Correct it before linking.",
                    )
            elif matches and not document:
                self.add_error("name", f"A similar record already exists: {matches[0].name}.")
        cleaned["normalized_name"] = normalized_name
        return cleaned

    def validate_unique(self):
        if self.existing_counterparty:
            return
        super().validate_unique()

    def _post_clean(self):
        super()._post_clean()
        if not self.existing_counterparty:
            return
        non_field_errors = self._errors.get("__all__")
        if not non_field_errors:
            return
        messages = " ".join(
            message for error in non_field_errors.as_data() for message in error.messages
        )
        if "unique_counterparty_primary_document" in messages:
            del self._errors["__all__"]

    def save(self, commit=True):
        if self.existing_counterparty:
            document = self.cleaned_data.get("primary_document") or ""
            changed_fields = []
            if document and not self.existing_counterparty.primary_document:
                self.existing_counterparty.primary_document = document
                self.existing_counterparty.person_type = (
                    Counterparty.PersonType.INDIVIDUAL if len(document) == 11 else Counterparty.PersonType.COMPANY
                )
                changed_fields.extend(["primary_document", "person_type"])
            if self.cleaned_data.get("default_category") and not self.existing_counterparty.default_category_id:
                self.existing_counterparty.default_category = self.cleaned_data["default_category"]
                changed_fields.append("default_category")
            if commit and changed_fields:
                changed_fields.append("updated_at")
                self.existing_counterparty.save(update_fields=changed_fields)
                sync_primary_document(self.existing_counterparty)
            return self.existing_counterparty
        counterparty = super().save(commit=False)
        counterparty.kind = self.kind
        counterparty.normalized_name = self.cleaned_data["normalized_name"]
        counterparty.source = Origin.MANUAL
        counterparty.confidence = 1
        if counterparty.primary_document:
            counterparty.person_type = (
                Counterparty.PersonType.INDIVIDUAL
                if len(counterparty.primary_document) == 11
                else Counterparty.PersonType.COMPANY
            )
        else:
            counterparty.person_type = Counterparty.PersonType.UNKNOWN
        if commit:
            counterparty.save()
            self.save_m2m()
            sync_primary_document(counterparty)
        return counterparty


class WorkCostCenterQuickForm(forms.Form):
    work_name = forms.CharField(label="Project name", max_length=200)
    cost_center_name = forms.CharField(label="Cost center", max_length=150, initial="Project")
    city = forms.CharField(label="City", max_length=150, required=False)
    state = forms.CharField(label="UF", max_length=2, required=False)

    def __init__(self, *args, reuse_existing: bool = False, **kwargs):
        self.reuse_existing = reuse_existing
        self.existing_work = None
        super().__init__(*args, **kwargs)

    def clean_work_name(self):
        name = str(self.cleaned_data["work_name"]).strip()
        existing = Work.objects.filter(normalized_name=normalize_text(name)).first()
        if existing and self.reuse_existing:
            self.existing_work = existing
        elif existing:
            raise forms.ValidationError("A project with this name already exists.")
        return name

    def clean_cost_center_name(self):
        name = str(self.cleaned_data["cost_center_name"]).strip()
        if not name:
            raise forms.ValidationError("Enter the cost center.")
        return name

    def clean_state(self):
        return str(self.cleaned_data.get("state") or "").strip().upper()

    def clean(self):
        cleaned = super().clean()
        cleaned["normalized_work_name"] = normalize_text(cleaned.get("work_name"))
        cleaned["normalized_cost_center_name"] = normalize_text(cleaned.get("cost_center_name"))
        return cleaned

    def save(self):
        cost_center, _ = CostCenter.objects.get_or_create(
            normalized_name=self.cleaned_data["normalized_cost_center_name"],
            defaults={"name": self.cleaned_data["cost_center_name"]},
        )
        if self.existing_work:
            work = self.existing_work
            changed_fields = []
            if not work.is_active:
                work.is_active = True
                changed_fields.append("is_active")
            if work.status != Work.Status.ACTIVE:
                work.status = Work.Status.ACTIVE
                changed_fields.append("status")
            if changed_fields:
                changed_fields.append("updated_at")
                work.save(update_fields=changed_fields)
        else:
            work = Work.objects.create(
                name=self.cleaned_data["work_name"],
                normalized_name=self.cleaned_data["normalized_work_name"],
                city=self.cleaned_data.get("city", ""),
                state=self.cleaned_data.get("state", ""),
            )
        return cost_center, work


class BudgetImportForm(forms.Form):
    budget_file = forms.FileField(label="Budget spreadsheet (.xlsx)")

    def clean_budget_file(self):
        uploaded = self.cleaned_data["budget_file"]
        filename = str(uploaded.name or "").lower()
        if not filename.endswith(".xlsx"):
            raise forms.ValidationError("Upload a valid .xlsx file.")
        return uploaded


class OfxPaymentBulkEditForm(forms.Form):
    category = forms.ModelChoiceField(
        label="Category",
        queryset=Category.objects.none(),
        required=False,
    )
    cost_center = forms.ModelChoiceField(
        label="Cost center",
        queryset=CostCenter.objects.none(),
        required=False,
    )
    work = forms.ModelChoiceField(
        label="Project",
        queryset=Work.objects.none(),
        required=False,
    )
    payment_method = forms.CharField(label="Payment method", max_length=80, required=False)
    payer = forms.CharField(label="Payer", max_length=120, required=False)
    bank_account = forms.CharField(label="Bank account", max_length=160, required=False)
    payment_status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=[
            ("", "Keep current"),
            (Payment.Status.CORRECTING, "Correcting"),
            (Payment.Status.PENDING_CONFIRMATION, "Pending approval"),
        ],
    )
    clear_work_if_company = forms.BooleanField(
        label="Clear project when cost center is Company",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["cost_center"].queryset = CostCenter.objects.filter(is_active=True).order_by("name")
        self.fields["work"].queryset = Work.objects.filter(is_active=True).order_by("name")

    def clean_payment_method(self):
        return str(self.cleaned_data.get("payment_method") or "").strip()

    def clean_payer(self):
        return str(self.cleaned_data.get("payer") or "").strip()

    def clean_bank_account(self):
        return str(self.cleaned_data.get("bank_account") or "").strip()

    def clean(self):
        cleaned = super().clean()
        cost_center = cleaned.get("cost_center")
        work = cleaned.get("work")
        clear_work = cleaned.get("clear_work_if_company")
        if clear_work and work:
            self.add_error("clear_work_if_company", "Choose either setting a project or clearing the current project.")
        if clear_work and not cost_center:
            self.add_error("cost_center", "Select the Company cost center to clear the project.")
        if clear_work and cost_center and normalize_text(cost_center.name) != normalize_text("Company"):
            self.add_error("cost_center", "To clear the project, the cost center must be Company.")
        return cleaned

    def has_bulk_changes(self) -> bool:
        if not self.is_valid():
            return False
        return any(
            [
                self.cleaned_data.get("category"),
                self.cleaned_data.get("cost_center"),
                self.cleaned_data.get("work"),
                self.cleaned_data.get("payment_method"),
                self.cleaned_data.get("payer"),
                self.cleaned_data.get("bank_account"),
                self.cleaned_data.get("payment_status"),
                self.cleaned_data.get("clear_work_if_company"),
            ]
        )
