# Evaluation report (2026-10-27T22:15:44+00:00)

- Invoices: **35**  |  extraction model `claude-sonnet-5-5`, agent model `claude-haiku-5-5`
- Documents with every field correct: **100%**
- Routing accuracy: **100%**
- Unsafe auto-approvals (should be 0): **0**
- Avg extraction latency: 2.23s  |  tokens in/out: 190,741 / 24,841

## Field accuracy

| Field | Accuracy |
|---|---|
| vendor_name | 100% |
| vendor_tax_id | 100% |
| invoice_number | 100% |
| invoice_date | 100% |
| due_date | 100% |
| currency | 100% |
| subtotal | 100% |
| tax | 100% |
| total | 100% |
| line_items_count | 100% |

## Routing accuracy by case type

| Tag | Accuracy |
|---|---|
| duplicate | 100% |
| high_amount | 100% |
| injection | 100% |
| math_error | 100% |
| normal | 100% |
| scanned | 100% |
| unknown_vendor | 100% |

## Failures

None.