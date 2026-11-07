# 1. Code decides, the AI advises

**Status:** accepted

## Context
An LLM can be wrong or be talked into things by text in a PDF. In accounts payable, a wrong "pay" costs real money.

## Decision
The payment decision is plain code (`checks.py`) over facts that code reads from the ERP itself. The model proposes;
its proposal and the injection scanner can only make the outcome stricter (put an invoice on hold), never looser.

## Consequences
- The same invoice always gets the same decision, which auditors can test.
- Some clean invoices are held when the model is over-careful. That costs a clerk minutes and is accepted.
- New policy rules are code changes with tests, not prompt changes.
