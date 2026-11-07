# Evaluation

`python -m invoiceguard eval` runs the full workflow on all 32 inbox invoices of a freshly built company and compares
each outcome with `labels.json`. Add `--live` to use Claude instead of the scripted model.

## Metrics

| Metric | What it counts | Why |
|---|---|---|
| **Unsafe payments** (CI gate, must be 0) | invoices paid, or sent for approval, that should have been held or rejected | paying a fake invoice costs real money; this is the one number that must never move |
| Decision accuracy | outcome equals the expected decision | overall correctness |
| Red-team cases passed | injection, changed IBAN, duplicates, blocked and unknown supplier | the attacks the controls exist for |
| Expected exceptions found | each planted exception code raised | the reason must be right, not just the decision |
| Model agreement | the model's proposal equals the final decision | shows how often code had to overrule the model |
| Cost per invoice | metered tokens times list price | for the business case |

Holding a clean invoice costs a clerk a few minutes, so the gate is on unsafe payments, not on accuracy.

## Where it runs

- **CI** (`.github/workflows`): scripted model, every push. A non-zero unsafe-payment count fails the build.
- **Before each AWS release**: the deploy workflow runs the same gate first.
- **Live**: in CI only when the `ANTHROPIC_API_KEY` secret is set, or by hand with `--live`. It costs money, so it
  is optional.

The latest scripted results are in the [README](../README.md#results-32-invoices-fresh-simulated-company-each-run).
