# Documentation index

Start with the document that matches your role. All guides describe the current implementation; when a detail conflicts with the code, the code is authoritative.

## Core guides

| Document | Audience | Purpose |
|---|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Engineers, architects | Layering, ingestion state machine, query chain, isolation mechanisms, ports and data model. |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Platform and DevOps engineers | Local Compose stack, migrations, environment variables, demo deployment, backup and rollback. |
| [`OPERATIONS.md`](OPERATIONS.md) | On-call and SRE | Probes, job diagnosis, manual retry, stale-job recovery, embedding drift remediation, failure modes, logs and load testing. |
| [`SECURITY.md`](SECURITY.md) | Security reviewers, platform engineers | Trust boundary, gateway controls, RLS and ACL design, prompt-injection screening, secrets handling, demo caveats. |
| [`EVALUATION.md`](EVALUATION.md) | Engineers, QA | Dataset, metric definitions, current results, refusal calibration, regression gate and limitations. |
| [`API.md`](API.md) | API consumers, integrators | Endpoint map, request conventions, SSE frames and error semantics. `/docs` remains the authoritative contract. |
| [`ROADMAP.md`](ROADMAP.md) | Product and engineering leads | Prioritised gaps with acceptance criteria, without delivery dates. |

## Related material

| Document | Audience | Purpose |
|---|---|---|
| [`../README.md`](../README.md) | Everyone | Project overview, capabilities, quickstart, API and evaluation summary. |
| [`runbook.md`](runbook.md) | On-call and SRE | Degradation drills with recorded evidence: parser loss and pypdf fallback, embedding key removal, database outage. |
| [`adr/README.md`](adr/README.md) | Engineers, architects | Index of architecture decision records covering retrieval, storage, chunking, isolation, versioning, ingestion, OCR and the cockpit. |
| [`../SECURITY.md`](../SECURITY.md) | External reporters | Vulnerability reporting process, supported versions and response expectations. |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Everyone | Released and unreleased changes in Keep a Changelog format. |

Additional design notes live alongside this index: `chunking-design.md`, `query-rewrite.md`, `retrieval-comparison.md`, `vector-store-design.md`, `ops-cockpit-checklist.md` and `ui-checklist.md`, with implementation plans under `plans/` and specifications under `superpowers/specs/`. They are historical working documents and may describe earlier states of the system.
