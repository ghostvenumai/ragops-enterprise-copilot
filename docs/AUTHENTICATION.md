# Authentication and identity (Phase 2)

The API now has an explicit `IdentityProvider` boundary and immutable
`AuthenticatedUserContext`. OIDC requests derive subject, tenant and roles from
the verified JWT. Client body fields are ignored in OIDC mode. The development
provider is available only in local, development, test and demo modes; legacy
body selectors are accepted only there for the synthetic dashboard.

Setting `RAGOPS_ENV=production` fails before the application creates its demo
workflow, loads fixtures or opens audit files. This is an explicit release block,
not an authentication implementation. Unknown environment values fail as well.

OIDC validates an asymmetric signature, approved algorithms, issuer, audience,
expiry and required `sub` claim. `tenant_id` and `roles` are required by default
and configurable for provider-specific claim layouts. `name`, `email` and
`preferred_username` are optional display fields. The public key is injected
through configuration; key rotation belongs to the deployment control plane.

Trust order is: bearer authentication -> verified claims -> tenant boundary ->
RBAC/admin check -> resource access -> retrieval. Admin is tenant-scoped.

The implementation is provider-neutral. Example placeholders:

```dotenv
RAGOPS_IDENTITY_PROVIDER=oidc
RAGOPS_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
RAGOPS_OIDC_AUDIENCE=<application-client-id>
RAGOPS_OIDC_ROLES_CLAIM=roles
RAGOPS_OIDC_TENANT_CLAIM=tid
RAGOPS_OIDC_ALGORITHMS=RS256
RAGOPS_OIDC_PUBLIC_KEY=<secret-mounted-public-key>

# Keycloak, Auth0 and generic OIDC use the same settings with their issuer,
# audience and claim names substituted.
```

Required negative tests include forged, expired, wrong-issuer/wrong-audience
tokens, tenant manipulation, role escalation and unauthenticated admin access.
External provider and production integration remain NOT_EXECUTED where the
sandbox cannot provide them. Unit tests exercise signed RSA JWTs and all
required negative claim/signature cases.

Threat assumptions: the issuer and signing-key configuration is provisioned by
the deployment operator, TLS protects token delivery, and bearer tokens may be
stolen. The API therefore validates every request, never trusts client identity
fields, and relies on short token lifetimes and upstream key rotation for
revocation. Compromised issuer keys or an already authorized administrator are
outside this component's boundary and require provider-side response.

## Hardening settings (ENT-10.9)

| Setting | Default | Effect |
| --- | --- | --- |
| `RAGOPS_OIDC_ALLOWED_CLIENTS` | empty | Comma-separated client IDs. When set, the token's authorized party (`azp`, else `client_id`) must be one of them. |
| `RAGOPS_PLATFORM_ADMIN_TENANT_ID` | unset | Only admins of this tenant may change the shared model catalog; unset disables catalog changes. |
| `RAGOPS_MAX_REQUEST_BYTES` | `3000000` | Request bodies above this size are rejected with 413 before parsing. |

Independently of these settings, only access tokens are accepted: ID, refresh and
logout tokens are rejected by their `typ` header or claim. In production the
interactive API documentation and the OpenAPI schema are not served.

The local Keycloak product client `ragops-api` has no resource-owner password grant.
The local `oidc` gate uses the separate, gate-only client `ragops-integration-check`;
see [RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md) and the control matrix in
[THREAT_MODEL.md](THREAT_MODEL.md).
