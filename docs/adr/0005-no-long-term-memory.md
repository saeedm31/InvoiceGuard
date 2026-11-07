# 5. No long-term memory

**Status:** accepted

## Context
Agent memory ("this supplier is always fine") is a known way to poison later decisions.

## Decision
Each invoice starts from ERP facts only. Nothing the model writes is kept for the next case. What the business learns
goes into the vendor master or the policy code, through people.

## Consequences
- One bad case cannot change how later invoices are handled.
- Repeat exceptions for the same supplier are handled again each time until the master data is fixed.
