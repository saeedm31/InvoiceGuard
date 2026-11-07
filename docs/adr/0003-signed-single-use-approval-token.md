# 3. Payments need a signed, single-use approval token

**Status:** accepted

## Context
Even with code deciding, a bug or a fooled client could call `schedule_payment`. The last line of defence should be
in the ERP server, not in the agent.

## Decision
Code mints an HMAC-signed token only after the policy passes or a person within their approval limit approves. The
token is bound to one invoice, one amount and 15 minutes, and can be used once. The ERP server checks the token, the
supplier status and the IBAN again, under a write lock, before it pays.

## Consequences
- Two approvals at the same moment create one payment, not two.
- The model never sees a token, so it cannot pass one on.
- `APPROVAL_SECRET` must be managed as a secret (Secrets Manager in AWS).
