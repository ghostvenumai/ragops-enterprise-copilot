# Threat Model

## Method

STRIDE-oriented assessment for local portfolio runtime.

| STRIDE | Attack Surface | Control | Residual Risk | Test Evidence |
| --- | --- | --- | --- | --- |
| Spoofing | Tenant or role fields in requests | RBAC and tenant filters | Demo auth is simplified | `tests/security/test_mandantentrennung.py` |
| Tampering | Uploaded file paths | Path traversal and extension checks | Binary parser adapters are local fallback | `tests/unit/test_security_controls.py` |
| Repudiation | Query handling | Correlation ID and audit events | Local logs are file-backed | Integration workflow tests |
| Information disclosure | Cross-tenant retrieval | Tenant filter before scoring | Admin role needs production IAM | Security tests |
| Denial of service | Large uploads, long inputs and request floods | Size and input limits, Docker limits, per tenant/user/endpoint-class rate limits enforced atomically in Redis (fail closed) | Fixed window allows up to 2x the limit across a window edge | Docker config check, rate_limiting RC gate |
| Elevation of privilege | Prompt injection in documents | Detection, penalty, evidence filtering | Pattern-based detection is incomplete | Prompt-injection tests |

## Required Defensive Test Cases

- Ignore previous instructions.
- Cross-tenant data request.
- System prompt disclosure.
- Fake administrator override.
- Obfuscated exfiltration language.
- Source-priority manipulation.
- Indirect injection in support ticket.

Synthetic fixtures under `data/synthetic/documents` cover these cases.

| Required case | Regression evidence |
| --- | --- |
| Ignore previous instructions | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected`, `tests/integration/test_workflow.py::test_workflow_blocks_user_prompt_injection` |
| Cross-tenant data request | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected`, `tests/security/test_mandantentrennung.py::test_cross_tenant_retrieval_is_blocked` |
| System prompt disclosure | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected`, `tests/unit/test_security_controls.py::test_prompt_injection_patterns_are_detected` |
| Fake administrator override | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected` |
| Obfuscated exfiltration language | `tests/unit/test_prompt_injection.py::test_letter_spacing_evasion_is_caught_by_compact_matching`, `tests/unit/test_prompt_injection.py::test_zero_width_and_invisible_characters_do_not_bypass_detection`, `tests/unit/test_prompt_injection.py::test_base64_encoded_instruction_is_decoded_and_detected` |
| Source-priority manipulation | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected` (citation-manipulation pattern group) |
| Indirect injection in support ticket | `tests/unit/test_ingestion_retrieval.py::test_ingestion_extracts_metadata_and_prompt_injection_flags`, `tests/unit/test_prompt_injection.py::test_hidden_instruction_inside_html_comment_is_detected` |

All of these tests are evidence of control SEC-08 or SEC-04 below.

## Trust Boundaries and Assets

| Boundary | Untrusted side | Trusted side | Controls |
| --- | --- | --- | --- |
| Browser to TLS edge | Any network client | Caddy reverse proxy | CFG-01, CFG-02, LIVE-08 |
| Edge to API | HTTP request, headers, body, bearer token | FastAPI routes behind `RequestHardening` | SEC-01, SEC-02, SEC-10, SEC-11, LIVE-05 |
| Identity provider to API | Token claims | `OIDCIdentityProvider` with configured key, issuer, audience | SEC-01, SEC-02, SEC-03, LIVE-01, LIVE-02 |
| Tenant to tenant | Identifiers and filters chosen by a caller | Tenant-scoped repositories and `AuthorizedVectorScope` | SEC-04, SEC-05, SEC-06, LIVE-03, LIVE-04 |
| Uploaded document to storage and index | File name, bytes, embedded text | Upload validation, structural inspection, injection flags | SEC-07, SEC-08 |
| Application to audit and telemetry | Request content | Masked, correlated audit events | SEC-09 |
| Backup artifact to restore target | Artifact bytes, key file | Authenticated decryption, fresh-target checks | SEC-12, LIVE-06 |
| Configuration and supply chain | Environment, dependency set, repository files | Fail-closed settings, pinned dependencies | SEC-13, CFG-03, CFG-04, LIVE-07, LIVE-09 |

Protected assets: tenant documents and vectors, CRM records, identities and roles,
the shared model catalog, budgets and usage records, backup sets and their key, and
the audit trail. Only synthetic data is used in this repository.

## Control Matrix

The security release gate (`scripts/security_gate.py`, gate `security` of
`make rc-live-gate`) has 26 mandatory controls: 17 of type `regression`
and 9 of type `live`. The table is generated with
`python scripts/security_gate.py --matrix`; a test keeps this document and the gate
identical.

- `regression` evidence is produced by the gate itself on the tested commit: the named
  tests are run once, or a read-only check inspects repository configuration.
- `live` evidence was produced on the integration host: the sanitized result another RC
  gate wrote under `evidence/product-v1/rc-live/`, or the settings of the running local
  Keycloak realm read through its admin API.

| ID | STRIDE | Mandatory control | Type | Evidence source |
| --- | --- | --- | --- | --- |
| SEC-01 | Spoofing | Bearer tokens are verified (signature, issuer, audience, claims); client-supplied identity is ignored | regression | `tests/security/test_identity.py::test_invalid_claims_are_rejected`<br>`tests/security/test_identity.py::test_invalid_signature_and_malformed_token_rejected`<br>`tests/security/test_identity.py::test_forged_admin_role_requires_valid_signature`<br>`tests/security/test_identity.py::test_dependency_returns_401_without_bearer`<br>`tests/security/test_identity.py::test_dependency_rejects_invalid_bearer`<br>`tests/security/test_identity.py::test_client_identity_override_is_ignored_for_oidc` |
| SEC-02 | Spoofing | Only access tokens of approved clients are accepted (token-type confusion) | regression | `tests/security/test_token_type_confusion.py::test_non_access_token_types_are_rejected`<br>`tests/security/test_token_type_confusion.py::test_access_tokens_are_accepted`<br>`tests/security/test_token_type_confusion.py::test_logout_header_type_is_rejected`<br>`tests/security/test_token_type_confusion.py::test_authorized_party_allowlist_is_enforced_when_configured`<br>`tests/security/test_token_type_confusion.py::test_unknown_critical_header_extension_is_rejected` |
| SEC-03 | Spoofing | The product client has no password grant; the realm bounds login attempts | regression | `tests/product/test_keycloak_bootstrap_config.py::test_password_grant_is_limited_to_the_local_integration_check_client`<br>`tests/product/test_keycloak_bootstrap_config.py::test_client_settings_are_verified_after_bootstrap`<br>`tests/product/test_keycloak_bootstrap_config.py::test_realm_bootstrap_enables_bounded_brute_force_protection`<br>`tests/product/test_keycloak_bootstrap_config.py::test_cleanup_removes_only_the_gate_client_and_rejects_unknown_arguments` |
| SEC-04 | Information disclosure | Retrieval and vector queries are tenant- and access-filtered before scoring | regression | `tests/security/test_mandantentrennung.py::test_cross_tenant_retrieval_is_blocked`<br>`tests/unit/test_ingestion_retrieval.py::test_hybrid_search_enforces_tenant_and_access_filters`<br>`tests/product/test_vector_index.py::test_filter_enforces_tenant_scope_access_lifecycle_and_validity`<br>`tests/product/test_vector_index.py::test_workspace_and_collection_scope_cannot_be_broadened` |
| SEC-05 | Elevation of privilege | Administration requires the admin role and stays inside the admin's tenant | regression | `tests/security/test_admin_tenant_scope.py::test_model_policy_listing_is_scoped_to_admin_tenant`<br>`tests/security/test_admin_tenant_scope.py::test_audit_events_are_scoped_to_admin_tenant`<br>`tests/security/test_admin_tenant_scope.py::test_non_admin_cannot_read_admin_tenant_views`<br>`tests/security/test_product_foundation.py::test_admin_is_always_scoped_to_own_tenant`<br>`tests/security/test_identity.py::test_admin_dependency_rejects_non_admin_claim`<br>`tests/unit/test_security_controls.py::test_rbac_blocks_restricted_for_sales` |
| SEC-06 | Elevation of privilege | Only platform-tenant admins change the shared model catalog; unset fails closed | regression | `tests/security/test_catalog_platform_admin.py::test_tenant_admin_cannot_change_the_shared_catalog`<br>`tests/security/test_catalog_platform_admin.py::test_platform_admin_can_change_the_catalog`<br>`tests/security/test_catalog_platform_admin.py::test_catalog_changes_fail_closed_without_a_platform_tenant`<br>`tests/security/test_model_router_api.py::test_non_admin_cannot_simulate_or_change_routing` |
| SEC-07 | Tampering | Uploads are validated by name, path, signature and structure before storage | regression | `tests/unit/test_security_controls.py::test_safe_filename_blocks_path_traversal`<br>`tests/unit/test_security_controls.py::test_validate_document_path_blocks_escape`<br>`tests/security/test_product_foundation.py::test_hostile_upload_names_rejected`<br>`tests/unit/test_upload_signatures.py::test_real_pdf_and_docx_signatures_are_accepted`<br>`tests/unit/test_upload_signatures.py::test_mismatched_content_is_rejected`<br>`tests/unit/test_document_inspection.py::test_real_documents_are_accepted`<br>`tests/unit/test_document_inspection.py::test_unsafe_or_malformed_pdfs_are_rejected`<br>`tests/unit/test_document_inspection.py::test_unsafe_or_malformed_docx_archives_are_rejected`<br>`tests/unit/test_document_inspection.py::test_text_uploads_must_be_clean_utf8`<br>`tests/security/test_ingestion_upload_api.py::test_renamed_file_is_rejected_before_a_job_exists` |
| SEC-08 | Elevation of privilege | Instruction-like text in questions and documents is detected and never followed | regression | `tests/unit/test_prompt_injection.py::test_direct_injection_phrases_are_detected`<br>`tests/unit/test_prompt_injection.py::test_benign_text_is_not_flagged`<br>`tests/unit/test_prompt_injection.py::test_zero_width_and_invisible_characters_do_not_bypass_detection`<br>`tests/unit/test_prompt_injection.py::test_bidi_override_characters_are_stripped_before_matching`<br>`tests/unit/test_prompt_injection.py::test_letter_spacing_evasion_is_caught_by_compact_matching`<br>`tests/unit/test_prompt_injection.py::test_hidden_instruction_inside_html_comment_is_detected`<br>`tests/unit/test_prompt_injection.py::test_base64_encoded_instruction_is_decoded_and_detected`<br>`tests/unit/test_security_controls.py::test_prompt_injection_patterns_are_detected`<br>`tests/unit/test_ingestion_retrieval.py::test_ingestion_extracts_metadata_and_prompt_injection_flags`<br>`tests/integration/test_workflow.py::test_workflow_blocks_user_prompt_injection` |
| SEC-09 | Repudiation | Requests carry a correlation ID into audit events; PII is masked before storage | regression | `tests/integration/test_api.py::test_query_validation_metrics_and_audit`<br>`tests/unit/test_security_controls.py::test_pii_masking_redacts_email_and_phone` |
| SEC-10 | Denial of service | Oversized bodies are rejected before parsing; malformed input never causes a 5xx | regression | `tests/security/test_http_hardening.py::test_every_response_carries_security_headers`<br>`tests/security/test_http_hardening.py::test_oversized_body_is_rejected_before_parsing`<br>`tests/security/test_http_hardening.py::test_malformed_numbers_never_cause_server_errors`<br>`tests/security/test_http_hardening.py::test_budget_input_is_validated`<br>`tests/security/test_ingestion_upload_api.py::test_malformed_identifiers_are_not_found_not_server_errors` |
| SEC-11 | Denial of service | Every authenticated route is rate limited; a limiter outage fails closed | regression | `tests/security/test_rate_limit_api.py::test_query_route_enforces_limit_before_provider`<br>`tests/security/test_rate_limit_api.py::test_redis_outage_fails_closed_with_503`<br>`tests/security/test_rate_limit_api.py::test_every_authenticated_route_is_limited_and_exemptions_are_narrow`<br>`tests/product/test_rate_limit.py::test_backend_failures_raise_unavailable_not_allow`<br>`tests/product/test_rate_limit.py::test_redis_keys_are_pseudonymized_and_collision_free` |
| SEC-12 | Information disclosure | Backups are encrypted with an external private key; tampered sets are rejected | regression | `tests/product/test_data_backup.py::test_artifact_is_encrypted_and_key_is_external`<br>`tests/product/test_data_backup.py::test_corrupt_or_unauthenticated_artifacts_fail_before_target_mutation`<br>`tests/product/test_data_backup.py::test_error_messages_never_contain_plaintext`<br>`tests/product/test_data_backup.py::test_key_file_must_be_private_and_sized`<br>`tests/product/test_backup_key_bytes.py::test_raw_keys_with_whitespace_boundary_bytes_are_preserved`<br>`tests/product/test_backup_key_bytes.py::test_raw_key_with_a_trailing_newline_is_rejected_not_truncated` |
| SEC-13 | Elevation of privilege | Production configuration fails closed (no demo runtime, no provider fallback) | regression | `tests/security/test_product_foundation.py::test_production_cannot_start_demo_runtime`<br>`tests/security/test_product_foundation.py::test_invalid_environment_fails_closed`<br>`tests/security/test_product_foundation.py::test_provider_typo_does_not_fallback`<br>`tests/security/test_identity.py::test_development_provider_is_environment_bounded`<br>`tests/security/test_identity.py::test_oidc_configuration_fails_closed`<br>`tests/product/test_vector_index.py::test_production_vector_configuration_fails_closed` |
| CFG-01 | Information disclosure | The TLS edge sets security headers, hides the server and marks cookies Secure | regression | config check `edge_proxy_headers` (`scripts/security_gate.py`) |
| CFG-02 | Elevation of privilege | The edge container is read-only, drops capabilities and forbids new privileges; production selects OIDC | regression | config check `production_compose_hardening` (`scripts/security_gate.py`) |
| CFG-03 | Tampering | Direct dependencies are pinned and mirrored in constraints.txt | regression | config check `dependency_pins` (`scripts/security_gate.py`) |
| CFG-04 | Information disclosure | No environment, key or secret file is tracked in Git | regression | config check `secret_files_untracked` (`scripts/security_gate.py`) |
| LIVE-01 | Spoofing | A real Keycloak token is validated; a wrong audience is rejected | live | `evidence/product-v1/rc-live/oidc.json`: `status="PASS"` |
| LIVE-02 | Spoofing | The running realm has no password grant on ragops-api, bounds login attempts and disables self-registration | live | read-only Keycloak realm settings (`scripts/security_gate.py`) |
| LIVE-03 | Information disclosure | Tenant isolation holds across persistence, API, ingestion, vectors and FinOps | live | `evidence/product-v1/rc-live/tenant-isolation.json`: `status="PASS"`, `tenant_leakage=0`, `vector_tenant_leakage=0`, `unauthorized_access_count=0`, `cleanup_status="PASS"` |
| LIVE-04 | Elevation of privilege | Tenant admins cannot influence other tenants through the shared model catalog | live | `evidence/product-v1/rc-live/model-router.json`: `status="PASS"`, `router_tenant_leakage=0`, `cross_tenant_catalog_influence=false`, `paid_provider_calls=0` |
| LIVE-05 | Denial of service | Distributed rate limiting is atomic, tenant-isolated and fails closed on Redis | live | `evidence/product-v1/rc-live/rate-limiting.json`: `status="PASS"`, `atomicity_verified=true`, `redis_outage_verified=true`, `spoofed_identity_rejected=true`, `rate_limit_tenant_leakage=0`, `cleanup_status="PASS"` |
| LIVE-06 | Information disclosure | Encrypted backup and restore reject wrong keys, tampering and unsafe targets | live | `evidence/product-v1/rc-live/backup-restore.json`: `status="PASS"`, `encryption_enabled=true`, `key_external_to_artifact=true`, `wrong_key_rejected=true`, `tamper_rejected=true`, `backup_restore_tenant_leakage=0`, `secret_scan_passed=true`, `cleanup_status="PASS"` |
| LIVE-07 | Denial of service | Dependency outages turn the instance unready with sanitized reasons and no leakage | live | `evidence/product-v1/rc-live/readiness-failure-recovery.json`: `status="PASS"`, `readiness_payload_sanitized=true`, `rate_limit_fail_closed=true`, `readiness_recovery_tenant_leakage=0`, `secret_scan_passed=true`, `cleanup_status="PASS"` |
| LIVE-08 | Spoofing | Browser login, logout, session expiry, role denial and tenant isolation hold end to end | live | `evidence/product-v1/rc-live/browser-e2e.json`: `status="PASS"`, `real_browser_login_verified=true`, `logout_verified=true`, `session_expiry_verified=true`, `backend_rbac_denied=true`, `browser_e2e_tenant_leakage=0`, `sensitive_artifacts_detected=false`, `secret_scan_passed=true`, `cleanup_status="PASS"` |
| LIVE-09 | Tampering | The production Compose configuration is valid on the host | live | `evidence/product-v1/rc-live/docker-compose.json`: `status="PASS"` |

## Evidence Admissibility

The gate adds no probes and no input corpus of its own; it only decides whether the
existing evidence counts.

| Evidence state | Meaning | Effect |
| --- | --- | --- |
| `PASS` | Present, well-formed, for the tested commit, not older than 24 hours, every required field has exactly the required value and type | Control is evidenced |
| `MISSING` | File, field, test result or Keycloak access is absent; a named test was skipped; the source gate is `BLOCKED` | `security` stays `BLOCKED` |
| `MALFORMED` | Unreadable report, invalid JSON, missing or untyped `tested_commit`/`timestamp` | `security` stays `BLOCKED` |
| `STALE` | Evidence is for another commit, too old, or dated in the future | `security` stays `BLOCKED` |
| `FAIL` | A named test failed, a required field is false, a configuration check is not satisfied | `security` is `FAIL` |

Additional rules:

- Uncommitted changes in product paths keep the gate `BLOCKED`, because the evidence
  would not be attributable to the tested commit.
- A gate-only object that remains after the run (the regression work directory or the
  Keycloak client `ragops-integration-check`) is a `FAIL`.
- Evidence that contains a credential, token, private key or connection string is
  replaced by a minimal `FAIL` record.
- `15/15` is reported only when all 26 controls are evidenced. Controls are
  never removed, merged or weakened to reach that state.

## Identity Provider Clients

| Client | Purpose | `directAccessGrantsEnabled` | Lifetime |
| --- | --- | --- | --- |
| `ragops-api` | Audience and role container of the product API | `false` | Permanent |
| `ragops-integration-check` | Password grant for the synthetic `integration-user` in the local `oidc` gate | `true` | Local only; removed by `make oidc-integration-cleanup` and by the RC runner after the gate |

`scripts/configure_local_oidc_integration.py` snapshots the redacted realm
configuration (owner-only, under the ignored `.loop/tmp/`) before it changes anything,
verifies both client settings afterwards and aborts when anything it does not manage
has changed.

## Residual Risks

- Pattern-based injection detection is incomplete against adaptive phrasing; tenant
  isolation does not depend on it (`docs/SECURITY_NOTES.md`).
- The fixed rate-limit window allows up to twice the limit across a window edge.
- The Copilot query path answers from the local demo corpus; Qdrant-backed answers are
  covered by the vector and tenant-isolation gates, not by the browser gate.
- The local Keycloak realm runs in development mode over loopback HTTP; production TLS
  and identity-provider hardening are the operator's responsibility.
- Regression and live evidence are not a security certification or a penetration test.
