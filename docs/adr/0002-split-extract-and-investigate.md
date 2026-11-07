# 2. Separate the model that reads the PDF from the model that uses tools

**Status:** accepted

## Context
Supplier PDFs are untrusted. A model that reads them and also has tools could be steered to call those tools.

## Decision
Two model calls. **Extract** reads the PDF text and fills a fixed schema; it has no tools. **Investigate** has
read-only ERP tools but sees only the extracted fields, never the raw document.

## Consequences
- A hidden instruction can at worst produce wrong fields, and every field is checked against the ERP.
- Two calls cost more than one. To balance that, the explain step uses a smaller model (`WRITER_MODEL`).
