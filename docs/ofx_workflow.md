# OFX Operational Workflow

This guide explains how OFX bank statements are used as the banking source of truth for payment review. The goal is to reduce manual typing, find inconsistencies, and close each month with stronger controls, without approving anything automatically.

## When To Import OFX

Import the OFX file when the bank statement for the period is available, preferably during monthly close or weekly reviews.

Recommended workflow:

1. Record expenses during the month through Telegram or the web interface.
2. At close, import the period OFX in the OFX review screen.
3. Review the suggestions created from OFX transactions.
4. Correct cost center, project, category, and vendor/worker when needed.
5. Approve only reviewed payments.
6. Open monthly close and generate spreadsheets only when there are no blockers.

## Telegram, Manual Entries, And OFX

Telegram:

- Used to send receipts, text, images, or PDFs at payment time.
- Can generate drafts and payment suggestions.
- Always requires review and confirmation before the payment is approved.

Manual entry:

- Used when a payment needs to be created or corrected directly in the web interface.
- Useful for expenses without receipts, adjustments, or entries that did not come through Telegram.
- Also goes through review and approval according to its status.

OFX:

- Banking source used to validate what left the account.
- Can create suggested `Payment` records for expenses that were not entered yet.
- Can identify taxpayer IDs, payee names, amount, date, payment method, and possible duplicates.
- Must never approve a payment by itself.

## Review OFX Transactions

Open:

```text
/interno/ofx/pendings/
```

Use month, year, and status filters to review:

- `Missing payment`: bank expense without a related `Payment`.
- `With suggested payment`: OFX transaction that already generated a suggestion.
- `Pending registration`: suggestion without confirmed vendor/worker.
- `Pending approval`: suggestion ready for human review and approval.
- `Possible duplicate`: may already exist as an entry.
- `Divergent`: amount, date, or counterparty mismatch.
- `Ignored credit`: credit/income outside the expense workflow.

When reviewing a row, check date, amount, payee, extracted taxpayer ID, memo summary, OFX status, and related payment.

## Register A Pending Vendor Or Worker

When an OFX suggestion is `Pending registration`:

1. Open the OFX review screen.
2. Find the transaction.
3. Use quick registration for vendor or worker.
4. Review the suggested name and CPF/CNPJ.
5. Save the record.
6. Return to OFX review and continue classifying the payment.

Practical rule:

- Company, shop, legal-entity service provider, or materials: usually a vendor.
- Individual person paid for field work: usually a worker.
- If there is doubt, keep it pending until confirmed.

## Bulk Edit Cost Center And Project

Use bulk edit when several OFX suggestions share the same classification.

Examples:

- Several material payments for the same project.
- Taxes or bank fees that belong to the company cost center.
- Several labor payments for the same project.

Steps:

1. Filter the OFX screen by period and `With suggested payment`.
2. Select the payments that should receive the same classification.
3. Fill shared fields such as category, cost center, project, payment method, payer, or bank account.
4. Apply the bulk edit.
5. Review the results before approval.

Important checks:

- If a project is selected, the cost center should usually be `Project`.
- If `Company` is selected, clear the project only when the expense is not project-related.
- Bulk edit does not approve payments.

## Bulk Approval

Bulk approval is for OFX-suggested payments that were already reviewed.

Approve in bulk only when:

- Status is `Pending approval`.
- Vendor/worker is filled.
- Category is filled.
- Cost center is filled.
- There is no duplicate or divergence.
- Required export fields are complete.

Do not approve in bulk when:

- Status is `Pending registration`.
- The transaction is marked as possible duplicate.
- There is a divergence.
- Required date is missing.
- The payment still needs a human decision.

## Possible Duplicates

A duplicate can happen when a payment was entered through Telegram or manually and later appeared in the OFX file.

How to handle:

1. Open the transaction marked as `Possible duplicate`.
2. Compare date, amount, and vendor/worker.
3. If it is the same expense, confirm or keep the correct reconciliation.
4. If not, correct the classification or create the right entry.
5. Do not bulk approve until the duplicate is resolved.

Rule: the same expense must not produce two exportable `Payment` records.

## Divergence

A divergence means the system found a conflict between the OFX transaction and an entry.

Examples:

- OFX shows R$ 1,250.00 but the entry shows R$ 1,200.00.
- Bank date differs from the informed payment date.
- Receipt payee does not match OFX payee.

How to handle:

1. Open the divergent transaction.
2. Check receipt, OFX memo, and related `Payment`.
3. Correct date, amount, counterparty, category, or cost center when needed.
4. Confirm reconciliation only when the date makes sense.

Unresolved divergences block monthly close.

## Credit Or Income

Credits and income do not enter the initial expense workflow.

When a credit appears:

- The system should mark it as ignored credit/income.
- It must not create a `Payment`.
- It must not block monthly close.
- It can be reviewed with the `Ignored credit` filter.

## Monthly Close

Open:

```text
/interno/fechamento/
```

Before generating spreadsheets, review the checklist:

- Active drafts.
- Payments pending registration.
- Payments pending approval.
- Payments under correction.
- OFX expenses without payment.
- Pending OFX suggestions.
- Duplicates.
- Divergences.
- Missing required fields.
- OFX imported for the period.
- Ignored credits.

Generation blockers:

- OFX expense without a `Payment`.
- Payment pending registration.
- Payment pending approval.
- Unresolved duplicate.
- Unresolved divergence.
- Missing required field in an exportable payment.

Warnings only:

- Ignored credit.
- Project without imported budget.
- Missing OFX when the statement has not been received yet.
- Approved payment without OFX when no OFX has been imported for the period.

When the checklist is clear, click `Generate spreadsheets` and download the accounting/export files.

## What Must Never Be Automatic

The system must never:

- Approve payments without human confirmation.
- Export pending payments.
- Create final vendor/worker records without confirmation when there is doubt.
- Overwrite human corrections with AI suggestions.
- Duplicate payments when the same OFX is reimported.
- Treat credits/income as expenses.
- Invent project, cost center, category, or budget item when confidence is low.
- Expose tokens, keys, passwords, `.env`, or sensitive date in logs, messages, or documentation.

## Monthly Routine Summary

1. Enter expenses through Telegram or the web during the month.
2. Import OFX at month end.
3. Review `Missing payment`, `Pending registration`, `Pending approval`, `Possible duplicate`, and `Divergent`.
4. Use bulk edit for cost center, project, and category when appropriate.
5. Approve only reviewed payments.
6. Open monthly close.
7. Resolve blockers.
8. Generate spreadsheets.
9. Send the files to the accountant or external system.
