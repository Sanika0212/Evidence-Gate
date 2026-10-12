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
| (b) Audit controls | Every decision is written as a hash-chained JSON record to an S3 bucket with Object Lock; Athena can query it | `gateway/audit_log.py`, `test_audit_records_form_a_valid_hash_chain` |
| (c)(1) Integrity | Hash chain detects any edited or removed record; Object Lock (governance mode) prevents changes during retention | `verify_chain`, bucket object-lock configuration |
| (d) Person or entity authentication | RS256 JWTs verified against the issuer's JWKS; forged, expired or wrong-issuer tokens are rejected | `test_rejects_missing_forged_and_expired_tokens` |
| (e)(1) Transmission security | TLS at the load balancer; S3 bucket policies deny non-TLS requests; NetworkPolicies limit which pods reach the internal gRPC services | `data.tf` (`DenyInsecureTransport`), `templates/policy.yaml` |

**Keeping PHI out of logs.** Checks run on the original text; every place that persists
text (audit records, the review queue, logs) receives redacted text from Presidio plus
pattern rules for structured identifiers (SSN, MRN, phone, email, dates, IP addresses).
Logs carry ids, counts and scores only. `test_pii_never_reaches_storage` seeds
identifiers and asserts that none reach any store.

**What this does not cover.** Administrative and physical safeguards, a business associate
agreement, and a risk analysis belong to whoever deploys it. The development issuer and
the demo page are for local use only and are disabled in `values-aws.yaml`.
