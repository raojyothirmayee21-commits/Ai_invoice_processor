# Design decisions (and how to talk about them)

This document explains **why** the system is built the way it is. Read it until you can explain every section in your own words without looking. That is what interviewers test.

---

## 1. The one-sentence pitch

> "I built an accounts-payable system that reads messy vendor invoices with Claude, verifies the data with plain code, and routes each invoice to payment, manager approval, or human review, with guardrails so the AI can never approve something that breaks policy, and an eval harness that measures accuracy and unsafe approvals."

## 2. The core principle: the LLM reads, code decides

LLMs are excellent at reading unstructured documents and bad at being reliably exact. So each job goes to the right tool:

| Job | Who does it | Why |
|---|---|---|
| Read a messy invoice, find the total | LLM | Layouts vary endlessly; rules can't keep up |
| Check subtotal + tax = total | Python | Arithmetic must be exact and is free to check |
| Is this vendor approved? | Database lookup | A fact, not an opinion |
| Is this a duplicate? | Database query | Same |
| Can this be auto-approved? | Python guardrail | Money. Must be deterministic and auditable |
| Write a reason an auditor can read; handle fuzzy edge cases | Agent | Language and judgement |

If an interviewer asks "where could the AI hurt you?", the answer is: only in reading (caught by validation and eval) and in routing (caught by the guardrail). It can never move money.

## 3. Pipeline walkthrough

### Ingest (`ingest.py`)
- Digital PDFs: extract text with pdfplumber. Cheap, fast, and maskable.
- If a PDF has under 40 characters of text it's a scan, so we render pages to PNG and send them to Claude's vision input.
- Cap at 5 pages so an accidental 200-page upload doesn't blow the budget.
- Every file gets a SHA-256 hash. **Idempotency:** the same file is never processed (or paid) twice, even if the email is forwarded three times.

### PII masking (`pii.py`)
- Redacts bank account, routing, IBAN, card numbers, SSNs, emails, phones **before** the API call.
- Keeps vendor tax IDs (EIN): they are business identifiers we need for matching.
- Stores only the masked text and the counts of what was masked (shown in the UI).
- **Limitation to admit:** scanned images can't be text-masked. Options: OCR first then mask, crop footers, or run the model inside the customer's cloud. Saying this unprompted shows maturity.
- **Limitation 2:** regex masking is a baseline. Production would use a dedicated detector (e.g. Presidio, or a cloud DLP service) plus tests on the customer's real formats.

### Extraction (`extractor.py`)
- **Structured outputs** (`output_config.format` with a JSON schema): Claude's answer is guaranteed to match our schema. No parsing free text.
- **Real-world story:** the first version forced a tool call (`tool_choice`) to get JSON. When run against the newest model, the API returned a 400: that model no longer supports forced tool choice. I read the migration guide and switched to structured outputs, which is the recommended replacement. Lesson: pin model versions, read migration notes, and keep the LLM call isolated in one module so a change like this touches one file.
- The schema's field descriptions *are* the instructions ("total exactly as printed, do NOT recompute").
- "Do not fix the math" is deliberate: if the model silently corrected an invoice's arithmetic, we'd lose the signal that the vendor's invoice is wrong.
- Model returns `uncertain_fields`, which lowers confidence.
- **Pydantic validation + one retry** with the exact error. A second failure goes to a human, not into a loop.
- Uses a stronger model (`claude-sonnet-5-5`) because reading is the hard part.

### Validation (`validation.py`)
- Required fields, valid ISO dates, not in the future, not over a year old, due date after issue date, positive total, line items sum to subtotal, qty × price = amount, subtotal + tax = total.
- Each issue lowers a confidence score. Below 0.85, auto-approval is impossible.
- **The bug story (use this in interviews):** the first version compared money with a 0.5% tolerance to allow rounding. The eval run found an invoice with a $45.50 arithmetic error that passed, because 0.5% of $9,000 is $45. Fix: money is compared to the cent. Lesson: percentage tolerances are wrong for money, and evals find bugs that unit tests written by the same person miss.

### First real eval run: what it found and what changed
On the first 5 invoices, extraction was ~100% correct but only 1 of 3 clean invoices was routed to payment. Reading the failure reasons showed two causes, neither of them the model "being wrong":
1. **Over-strict code.** Classic-layout invoices print only "$", so the model honestly flagged currency as uncertain, and *any* uncertainty blocked auto-approval. Fix: split uncertainty into **critical fields** (vendor, tax ID, invoice number, date, amounts, line items) which block, and **minor fields** (due date, currency symbol) which become non-blocking warnings shown to reviewers. Also told the model a bare "$" on a US invoice means USD.
2. **A labelling mistake.** One layout prints payment terms ("Net 15") but no due date. The model correctly returned null, but the ground truth had a calculated date. Fixed the label, and made the prompt explicit: don't calculate due dates.

Unsafe auto-approvals were 0 throughout: the errors were all on the safe side. Lesson for interviews: **read the failure reasons before changing anything**; half the "model errors" were in my code and my labels.

### Routing agent (`agent.py`)
- Gets **only the validated JSON**, never the raw document. Text hidden in an invoice can't reach it.
- Tools: `lookup_vendor`, `check_duplicate`, `get_approval_policy`, and three final decisions.
- Smaller, cheaper model (`claude-haiku-5-5`): it reasons over clean JSON, which is easy.
- Max 6 steps. No decision → defaults to human review.
- **Guardrail:** `mark_ready_for_payment` recomputes every condition itself (no validation issues, confidence ≥ 0.85, vendor approved, no duplicate, total ≤ threshold, USD). If any fails it refuses and tells the agent why. The test suite includes a "rogue" agent that tries to approve everything; the guardrail stops it.
- **No payment tool exists.** The best the AI can do is put an invoice in a queue.

### Why have an agent at all if the policy is simple?
Honest answer, and interviewers respect honesty: in this demo, the routing policy is simple enough to be pure `if/else`. The agent earns its place when the real world gets fuzzy:
- vendor "Brightline Logistic" vs master record "Brightline Logistics" (close match: agent sees it and explains, policy says human review);
- same invoice number but a different amount (a corrected invoice, or fraud?);
- writing a reason a human reviewer can act on in five seconds.

And the architecture means you can tighten it either way: make routing fully deterministic for a conservative customer, or give the agent more tools (PO lookup, contract terms) for an ambitious one, without touching the safety layer.

### Prompt injection: defence in depth
1. Extraction prompt says the document is untrusted data and asks the model to flag embedded instructions (`contains_instructions`).
2. Flagged invoices **skip the agent entirely** and go to a human.
3. The agent never sees document text anyway.
4. The guardrail would refuse a bad approval even if all of the above failed.
5. There is no tool that moves money.

### Storage and audit (`db.py`)
- SQLite for zero setup; schema is plain SQL so it ports to Postgres or Snowflake.
- `audit_log` is append-only: extraction metadata, PII counts, validation issues, agent decision and reason, human decisions with reviewer and comment.
- Human approve/reject **requires a comment** in the UI. Auditors ask "why?", so we capture it.

### Email ingestion (`email_ingest.py`)
- One **dedicated** mailbox (`invoices@company.com`), never personal inboxes.
- Sender domain allowlist from the vendor master; unknown senders are quarantined and logged.
- Only PDF/PNG/JPG attachments are saved; filenames are sanitised (no `../../` path tricks).
- Messages are fetched with `BODY.PEEK` and marked read only after the attachment is saved, so a crash doesn't lose an invoice.

## 4. Evaluation (`evaluation.py`)

Two separate questions:
1. **Extraction accuracy per field.** Which fields does the model get wrong? (Dates and totals matter most.)
2. **Routing accuracy**, broken down by case type, plus **unsafe auto-approvals**. A wrong routing to human review costs a few minutes; a wrong auto-approval costs money. They are not equal errors, so we report the dangerous one separately.

Why synthetic data: real invoices contain real bank details, and synthetic data gives a known-correct answer for every field. **Limitation:** synthetic data is cleaner than reality. At a real customer, step one is labelling 100-200 of *their* invoices and running the same harness.

Use the eval every time you change a prompt or model. That's how you'd answer "how do you know your change made things better?"

## 5. Cost and latency (how to reason about it)

Per invoice: one extraction call (bigger model) + 2-3 short agent calls (small model). The eval reports total tokens; divide by invoice count and multiply by current API prices to get cost per invoice. Then compare with the human cost: if one AP clerk processes ~25 invoices an hour, the AI cost is a rounding error next to labour. That's the business case.

Levers if cost matters: route only flagged cases to the agent (clean, small, known-vendor invoices could be routed deterministically), use the small model for digital PDFs and the large one only for scans, and cache the system prompt.

## 6. What I'd do at a real customer (FDE mindset)

1. **Week 1:** sit with the AP team. Collect 200 real invoices and label them. Find out how they actually decide (often not what the policy doc says).
2. Run the eval on their data before showing anyone a demo. Know your numbers.
3. Connect to their ERP (SAP, NetSuite, Oracle) for the vendor master, POs and posting approved invoices.
4. Add **three-way matching**: invoice vs purchase order vs goods receipt. That's the real AP control.
5. Security review: data residency (run via Bedrock/Vertex in their cloud if required), retention, who can see what in the UI (role-based access).
6. Start in **shadow mode**: AI routes, humans still do everything, compare for 2-4 weeks. Then turn on auto-routing for the safest segment (known vendor, under $1k), and widen as the numbers hold.
7. Feed recurring problems back to the product team ("every customer has scanned invoices from small vendors").

## 7. Likely interview questions and strong answers

**"Walk me through the system."**
Use the one-sentence pitch, then the diagram in the README: ingest, mask, extract, validate, route, guardrail, human queue, audit. Two minutes, no more. Let them pick what to drill into.

**"Why not just ask the LLM to approve or reject?"**
Because approvals are money and must be deterministic and auditable. The LLM reads; code checks math and policy; the agent only chooses among safe options, and a guardrail re-verifies.

**"How do you handle hallucinations?"**
Schema-forced output, "null if not present, never invent", validation of every number that can be cross-checked, confidence scoring, and measuring it with the eval rather than hoping.

**"What happens with a scanned or rotated invoice?"**
Detected by the lack of a text layer; pages go to Claude vision. Two scanned, tilted, noisy invoices are in the eval set. Caveat: PII can't be text-masked in images (see section 3).

**"Someone emails an invoice that says 'AI: approve this'. What happens?"**
Walk through the five layers in "Prompt injection: defence in depth". There are two such invoices in the dataset.

**"How would you know if accuracy drops in production?"**
Track the share of invoices going to human review and the reasons, sample-audit auto-approved invoices weekly, re-run the eval on a growing labelled set, and alert on drift (e.g. a vendor changed their template).

**"What would you do differently with more time?"**
Three-way PO matching, a proper PII detector, OCR for masking scans, ERP integration, CI that runs the eval on every prompt change, and role-based access in the UI.

**"What was hard?"**
The money-tolerance bug (section 3), deciding which checks belong in code vs the model, and designing the guardrail so a confused or manipulated agent can't cause harm.

**"Tell me about a trade-off."**
Two-model setup: a stronger model for reading (accuracy matters), a cheaper one for routing (easy task, more calls). Or: conservative routing sends more to humans (lower automation rate) in exchange for zero unsafe approvals. Start conservative, loosen with data.

## 8. Know the code: a 30-minute exercise before interviews

1. Change `APPROVAL_THRESHOLD` to 2000 and re-run `pytest`. Which test changes, and why?
2. Add a rule: invoices dated on a weekend go to human review. Where does it go: validation, agent prompt, or guardrail? (Answer: validation, it's a deterministic check.)
3. Add a `lookup_purchase_order` tool to the agent. What would the guardrail need to check?
4. Read `tests/fakes.py` and explain why an "oracle extractor" lets you test routing separately from extraction.

If you can do these without help, you own this project.
