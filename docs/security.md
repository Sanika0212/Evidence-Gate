# Security and HIPAA-style controls

Evidence Gate is a research prototype, not a certified product and not a covered entity's
system of record. It is built so that the technical safeguards of the HIPAA Security Rule
(45 CFR 164.312) have a concrete home. The table maps each safeguard to what implements
it and to the test or configuration that shows it.

| Safeguard (164.312) | Implementation | Evidence |
|---|---|---|
| (a)(1) Access control: unique user identification | OAuth2 client-credentials tokens per tenant client (Cognito in AWS, `devauth` locally); every audit record carries the `client_id` | `tests/test_auth.py`, `gateway/auth.py` |
| (a)(1) Access control: least privilege | One IAM role per service account (IRSA) with only the actions that service performs; scopes `check` and `label` on tokens | `deploy/terraform/aws/iam.tf`, `Principal.require` |
| (a)(2)(iv) Encryption at rest | S3 buckets, SQS queues, ECR and Secrets Manager use a customer-managed KMS key with rotation | `deploy/terraform/aws/main.tf`, `data.tf` |
| (b) Audit controls | Every decision is written as a hash-chained JSON record to an S3 bucket with Object Lock | `gateway/audit_log.py`, `test_audit_records_form_a_valid_hash_chain` |
| (c)(1) Integrity | Hash chain detects any edited or removed record; Object Lock (governance mode) prevents changes during retention | `verify_chain`, bucket object-lock configuration |
| (d) Person or entity authentication | RS256 JWTs verified against the issuer's JWKS; forged, expired or wrong-issuer tokens are rejected | `test_rejects_missing_forged_and_expired_tokens` |
| (e)(1) Transmission security | TLS at the load balancer; S3 bucket policies deny non-TLS requests; NetworkPolicies limit which pods reach the internal gRPC services | `data.tf` (`DenyInsecureTransport`), `templates/policy.yaml` |

**Keeping PHI out of storage, the queue and logs.** Checks run on the original text; every
place that persists or forwards text (audit records, the review queue, the triage queue
and so the triage model) receives redacted text. Pattern rules for structured identifiers
(SSN, record number, phone, email, dates, IP and street addresses) run first, then
Presidio with the large spaCy model, and a name recognised anywhere in a request is
redacted everywhere in it. Logs and traces carry ids, counts and scores only.

Two tests show it. `test_pii_never_reaches_storage` seeds identifiers into one request
and asserts none reach the audit store, the review store or the queue.
`eval/run_pii.py` sends 1,000 requests carrying 9,000 synthetic identifiers and scans
every sink, logs and traces included: 0 leaked (`runs/pii/results.json`). The first design
leaked 959 of them, mostly names and street addresses; that run is kept in
`runs/pii-first-version/` and the README explains each fix.

**What this does not cover.** Administrative and physical safeguards, a business associate
agreement, and a risk analysis belong to whoever deploys it. The development issuer and
the demo page are for local use only and are disabled in `values-aws.yaml`.
