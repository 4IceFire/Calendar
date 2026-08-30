# TDeck Kerberos and SSO Design

Status: proposal for review; no authentication implementation has been started.

## Executive recommendation

TDeck should support federated sign-in without implementing Kerberos or OpenID
Connect cryptography itself.

1. Add OpenID Connect (OIDC) Authorization Code Flow as TDeck's primary SSO
   interface. OIDC is the recommended path for iPhones, unmanaged devices, and
   users who already have an identity-provider browser session.
2. Keep Kerberos/SPNEGO ticket handling outside Flask. Prefer either:
   - an identity broker such as Keycloak that authenticates against Kerberos and
     sends a standard OIDC identity to TDeck; or
   - IIS Windows Authentication on Windows, or Apache `mod_auth_gssapi` on
     Linux, protecting one dedicated TDeck Kerberos-login endpoint.
3. Preserve TDeck's existing users, groups, device allow-lists, Flask-Login
   sessions, session revocation, and service tokens. SSO proves identity; TDeck
   continues to decide authorization.
4. Roll out in hybrid mode with at least one hardened local break-glass Admin
   account. Do not remove local password hashes during the initial rollout.

This design deliberately does not put a keytab, Kerberos ticket, domain password,
OIDC access token, or refresh token in TDeck.

## Why this fits TDeck

TDeck already has the right separation between authentication and authorization:

- `webui.py` uses Flask-Login to establish the browser identity.
- `auth.db` has users, groups, page grants, resource allow-lists, revocable
  `user_sessions`, active/locked state, and a per-user session version.
- Browser authorization and CSRF checks are centralized in `_auth_gate` and the
  API security policy.
- Automation uses scoped service tokens and must remain separate from human SSO.

A successful external authentication therefore only needs to resolve one stable
external identity to one local TDeck user and then enter the existing
`login_user(...)` plus `_create_user_session(...)` path.

There are several deployment constraints to address first:

- TDeck currently serves plain HTTP itself and defaults to `0.0.0.0`.
- Kerberos requires a stable DNS name and an `HTTP/<fqdn>` service principal; an
  IP-address URL is not a suitable production Kerberos identity.
- OIDC requires an exact, stable HTTPS callback URL.
- The current Flask signing key is instance configuration. Before federated
  login, it should move to a non-exported secret source, because it protects all
  logged-in sessions.
- Current sessions have an idle timeout but no absolute lifetime. Federated
  sessions need an absolute lifetime so a user disabled at the identity provider
  cannot retain a TDeck session indefinitely.

## Scope

### In scope

- OIDC sign-in against one configured provider.
- Optional Kerberos sign-in through an identity broker or trusted reverse proxy.
- Hybrid local-password plus SSO rollout and an SSO-required steady state.
- Safe creation/linking of external identities to TDeck users.
- Existing TDeck group authorization after SSO.
- Local and identity-provider logout behavior.
- Session provenance, auditing, revocation, and absolute lifetime.
- Configuration preflight, deployment documentation, and recovery procedures.

### Out of scope for the first release

- TDeck acting as an identity provider.
- SAML support; OIDC covers the required web SSO use case with less protocol
  surface.
- Accepting a user's AD password in TDeck.
- Kerberos delegation or impersonating users to downstream services.
- Automatic authorization from AD/OIDC group claims. TDeck groups remain the
  source of permissions for the first release.
- SSO for Companion or other automation. Those callers continue using scoped
  service tokens.
- Storing OIDC access/refresh tokens or calling Microsoft Graph/userinfo APIs
  after login.

## Options considered

| Option | Use | Decision |
| --- | --- | --- |
| OIDC directly from TDeck | Cross-platform browser SSO, including iPhone | Recommended |
| Keycloak Kerberos bridge to OIDC | On-premises AD/Kerberos with mixed desktop and mobile clients | Recommended when there is no existing OIDC provider |
| IIS Windows Authentication | Direct intranet SSO for Windows-hosted TDeck | Supported optional path |
| Apache `mod_auth_gssapi` | Direct intranet SSO for Linux/Docker-hosted TDeck | Supported optional path |
| In-process `pyspnego`/`python-gssapi` | Flask accepts SPNEGO tokens itself | Not recommended; it adds handshake, channel-binding, credential, and platform-specific security code to TDeck |
| Flask-Kerberos | Flask decorator extension | Rejected; its latest PyPI release is from 2014 and is marked beta |
| NTLM or Basic fallback | Compatibility when Kerberos fails | Rejected by default; never collect a domain password in TDeck |

The direct Kerberos option is for managed intranet clients. The upstream
`mod_auth_gssapi` documentation explicitly notes that Kerberos/NTLM are not
designed for public Internet authentication. IIS likewise describes Windows
Authentication as an intranet feature.

## Proposed architecture

### Preferred path: OIDC, optionally backed by Kerberos

```text
Managed desktop --Kerberos/SPNEGO--> Identity provider/broker
                                              |
iPhone or other browser --normal SSO--------->| OIDC Authorization Code
                                              v
                                  HTTPS reverse proxy
                                              |
                                              v
                                  TDeck OIDC callback
                                              |
                                   external identity -> local user
                                              |
                                              v
                              Existing TDeck session + groups
```

If the organization already uses Microsoft Entra ID or another OIDC provider,
TDeck connects to it directly. If it has only on-premises AD/Kerberos, Keycloak
can perform Kerberos/SPNEGO and present OIDC to TDeck. Keycloak documents both a
Kerberos bridge and OIDC clients.

### Optional direct Kerberos path

```text
Domain browser
    |
    | HTTPS + WWW-Authenticate: Negotiate
    v
IIS Windows Authentication or Apache mod_auth_gssapi
    |
    | authenticated principal + private proxy proof
    v
/auth/kerberos/complete (TDeck, backend-only trust boundary)
    |
    v
external identity -> local user -> existing TDeck session
```

Only the dedicated completion endpoint consumes the proxy assertion. Normal
TDeck pages continue to use the TDeck session, which avoids a Negotiate challenge
on every request and allows a local recovery login.

## Authentication modes

Add an explicit mode instead of overloading `auth_enabled`:

- `local`: current username/password behavior.
- `hybrid`: primary SSO button plus local login. This is the mandatory rollout
  mode.
- `sso_required`: SSO is primary and ordinary local accounts cannot log in.
  Explicitly designated break-glass local Admin accounts remain usable at a
  normal, rate-limited administrator-login route; there is no secret URL.

`auth_enabled=false` remains a development-only compatibility mode. Production
preflight should warn or fail when federated settings are present while
authentication is disabled.

## OIDC flow

1. The login page sends the user to `/auth/oidc/start` with a validated local
   return path.
2. TDeck creates transaction-specific `state`, `nonce`, and PKCE S256 values.
3. The browser is redirected to the provider using Authorization Code Flow.
4. The provider returns only to the exact registered HTTPS callback.
5. The selected library exchanges the code and validates the ID token signature,
   issuer, subject, audience/client ID, expiry, authorized party where relevant,
   nonce, and the transaction state/PKCE binding.
6. TDeck resolves the case-sensitive `(issuer, subject)` pair to an external
   identity. Email and display name are profile attributes, never identity keys.
7. TDeck rejects inactive/locked local users, rotates local session state, calls
   the existing Flask-Login/session creation path, and redirects only to a safe
   local path.
8. TDeck discards the authorization code, ID token, access token, and any client
   secret after the request. It does not request `offline_access`.

Use the minimal scopes `openid profile email`. Do not use implicit or hybrid
OIDC flows, wildcard callback URLs, a provider-supplied redirect destination, or
the email address as the unique identity.

### OIDC package selection

The default recommendation is a currently patched, exactly pinned Authlib
release. As of this design, `Authlib==1.7.2` supports Flask and Python 3.10-3.14
and contains fixes for recently disclosed OIDC issues. Restrict accepted ID-token
algorithms to the provider's configured asymmetric algorithm (normally RS256 or
ES256); reject `none`, symmetric ID-token algorithms, unconfigured algorithms,
and encrypted ID tokens in the first release.

If the review confirms that Microsoft Entra ID is the only provider TDeck will
ever support, use Microsoft's maintained MSAL for Python instead; the current
stable line supports Python 3.13 and provides the provider-specific authorization
code flow. Do not ship both libraries in the first release. Recheck advisories,
pin the selected stable version and transitive cryptography packages, and run a
dependency audit at implementation time.

## Direct Kerberos flow

1. `/login` offers **Use Windows sign-in**. Do not automatically issue a
   Negotiate challenge until deployment testing proves that all intended clients
   handle it cleanly.
2. The reverse proxy protects only `/auth/kerberos/complete` and performs the
   SPNEGO exchange using the operating system/IIS or `mod_auth_gssapi`.
3. The proxy supplies the canonical authenticated principal and a high-entropy
   proxy secret over the private backend connection. It must delete any incoming
   copies of those headers before setting its own.
4. TDeck accepts the assertion only on that endpoint, compares the proxy secret
   in constant time, validates the configured realm, rejects control characters
   and ambiguous/multiple identity headers, and maps the exact canonical
   principal to a local user.
5. TDeck creates its normal revocable browser session and immediately stops
   relying on the proxy identity header.

The backend port must bind to a dedicated loopback interface and be blocked from
the LAN. Loopback alone is not authentication; the proxy proof is also required.
For Apache, configure `GssapiAllowedMech krb5`, `GssapiSSLonly On`, Basic auth off,
and delegation/credential-cache export off. For IIS, enable TLS, Windows
Authentication and Extended Protection where client compatibility permits, and
verify with tickets/logging that Kerberos—not an unnoticed NTLM fallback—was
actually negotiated.

No Kerberos keytab is mounted in TDeck and no delegated user credential is
created. Apache/IIS or the identity broker owns the service credential and its
file/OS ACLs.

## iPhone behavior

OIDC is the default iPhone path. A browser that already has an identity-provider
session can return to TDeck without another TDeck password, and the provider can
apply MFA or Conditional Access.

Direct Kerberos can be seamless on Apple devices only when the deployment meets
Apple's Kerberos SSO extension requirements: the device is managed, receives an
Extensible SSO configuration profile, can reach the on-premises AD network (or
VPN), and receives an HTTP 401 Negotiate challenge for an allowed host. It is not
an Entra ID mechanism. An unmanaged iPhone should therefore not be a direct
Kerberos acceptance criterion.

## Identity and provisioning model

Add a separate identity-link table rather than placing external IDs in
`username` or `email`:

```sql
CREATE TABLE external_identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  provider_key TEXT NOT NULL,
  issuer TEXT NOT NULL,
  subject TEXT NOT NULL,
  principal TEXT,
  display_name TEXT,
  email TEXT,
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  UNIQUE(provider_key, issuer, subject),
  FOREIGN KEY(user_id) REFERENCES users(id)
);
```

- OIDC identity key: exact `issuer` plus case-sensitive `sub`.
- Direct Kerberos identity key: configured provider/realm plus exact canonical
  principal returned by the Kerberos acceptor. Do not blindly lowercase a
  Kerberos principal.
- Store only the allow-listed profile fields above. Do not store a raw claims
  JSON document.

Add `users.local_login_enabled INTEGER NOT NULL DEFAULT 1`. Existing users retain
local login. External-only users receive an unusable random password hash to
satisfy the current schema and have `local_login_enabled=0`; the local login and
password-reset paths must enforce that flag.

Add session provenance to `user_sessions`:

- `auth_method`: `local`, `oidc`, or `kerberos`.
- `external_identity_id`: nullable link to the identity used.
- `idp_session_id`: nullable OIDC `sid` for a later back-channel logout phase.
- Continue using the existing `created_at`, `last_seen_at`, `revoked_at`, and
  `session_version` controls.

### Provisioning policy

Default to just-in-time creation in a **pending/no-access** state, or disable JIT
entirely if administrators prefer pre-provisioning. A newly seen identity must
receive no TDeck groups. An administrator reviews it, assigns local groups, and
can link it to an existing account.

Never automatically link to an existing TDeck account based only on matching
username or email. Even a verified email is an attribute that can be reassigned;
linking is an explicit administrator action. Do not consume AD/OIDC group claims
for authorization in the first release. This prevents an identity-provider claim
or mapping mistake from silently granting VideoHub, mixer, Config, or Admin
control.

An external-only user with no groups gets a dedicated **Access pending** page,
not the current password-change landing page.

## Configuration and secret storage

Non-secret settings may remain in `config.json` and its Config UI:

- authentication mode and enabled provider(s);
- public external URL, for example `https://tdeck.example.org`;
- OIDC issuer/discovery URL, client ID, scopes, and allowed signing algorithms;
- optional JIT policy and display label;
- allowed Kerberos realm(s);
- idle and absolute session lifetimes.

Secrets must come from environment variables, Docker secrets, or an ignored file
with restricted OS permissions:

- `TDECK_OIDC_CLIENT_SECRET` (if symmetric client authentication is selected);
- `TDECK_AUTH_PROXY_SECRET` for the direct Kerberos proxy boundary;
- `TDECK_FLASK_SECRET_KEY` for Flask session signing.

The implementation should prefer `TDECK_FLASK_SECRET_KEY` and migrate away from
exporting the existing signing key in `config.json`. Secret values must never be
returned by Config APIs, included in config export/import, rendered in HTML,
included in exception text, or written to the Activity Log. Rotation of the
Flask key intentionally logs everyone out and must be a documented operation.

Use an explicit configured external URL to construct OIDC callbacks. Do not
trust arbitrary `Host` or forwarded headers. If proxy-aware request metadata is
needed elsewhere, configure an exact proxy count and reject untrusted hosts.

## Session and logout policy

- Set `Secure`, `HttpOnly`, and `SameSite=Lax` on the TDeck session cookie in
  federated deployments. Terminate TLS at the trusted proxy and enable HSTS
  after the hostname/certificate are stable.
- Clear pre-login session data, generate a new CSRF token, and create a new
  `user_sessions` row after every successful authentication.
- Preserve existing idle timeout and session-version revocation.
- Add an absolute session lifetime, recommended default eight hours, enforced
  from `user_sessions.created_at`. This applies even to groups that disable idle
  timeout.
- Local logout revokes the current TDeck session. OIDC provider logout is an
  explicit separate option so users are not unexpectedly signed out of every
  organizational app.
- Support RP-initiated OIDC logout when the provider advertises it. Treat OIDC
  back-channel logout as a later hardening phase; it requires validating signed
  logout tokens and revoking matching `sid`/subject sessions.
- A valid SSO identity never overrides a TDeck user's inactive or locked state.

## Security boundaries and mitigations

| Risk | Required mitigation |
| --- | --- |
| Spoofed reverse-proxy user header | Private backend bind, firewall/ACL, proxy strips client headers, separate high-entropy proxy secret, constant-time comparison, dedicated completion endpoint |
| Kerberos/NTLM relay | HTTPS, correct SPN/FQDN, IIS Extended Protection or GSSAPI SSL-only controls, no delegation, prove the negotiated mechanism |
| OIDC response replay or login CSRF | One-time state, nonce, PKCE S256, exact callback, short transaction expiry |
| OIDC mix-up or forged token | One configured issuer per callback, discovery issuer match, signature/JWKS, exact issuer/audience/authorized-party checks, explicit algorithm allow-list |
| Open redirect | One helper accepting only a local path beginning with one `/`; reject schemes, `//`, backslashes, control characters, and external hosts |
| Unsafe account linking | Stable issuer+subject/principal key; no automatic email/username linking; admin-reviewed linking |
| SSO claim grants hardware access | Keep all authorization in existing TDeck groups and resource allow-lists |
| IdP account disabled while TDeck session lives | Absolute session lifetime, local revocation, later OIDC back-channel logout if required |
| IdP outage locks out administrators | At least one strong local break-glass Admin, hybrid rollout, documented console recovery |
| Token/ticket leakage | Never store tokens/tickets, allow-list logged fields, sanitize library exceptions, keep secrets outside exported config |
| Dependency vulnerability | Exact pins, hash-locked deployment dependencies if practical, `pip-audit`/Dependabot review, explicit algorithm restrictions |

## UI and administration changes

### Login page

- Primary **Continue with SSO** button.
- Optional **Use Windows sign-in** button only when the direct Kerberos route is
  configured.
- Local fields remain in hybrid mode under **Use a local TDeck account**.
- Preserve the password-manager markup already added to the local form.
- Show generic errors. Put correlation IDs and sanitized detail in the Activity
  Log; never display or log protocol tokens.

### Permissions and user detail

- Show account sources: Local, OIDC provider, and/or Kerberos principal.
- Show last external sign-in and local-login enabled state.
- Let an Admin approve a pending identity, link/unlink it, and assign existing
  TDeck groups.
- Require recent authentication for linking/unlinking or changing break-glass
  status.
- Prevent removal/disablement of the last active break-glass Admin.
- Hide password change/reset controls for external-only accounts and explain that
  credentials are managed by the identity provider.

### Activity Log

Add stable actions such as:

- `auth.oidc.start`, `auth.oidc.success`, `auth.oidc.failure`;
- `auth.kerberos.success`, `auth.kerberos.failure`;
- `auth.identity.provision`, `auth.identity.link`, `auth.identity.unlink`;
- `auth.session.absolute_timeout` and `auth.config.preflight`.

Log provider key, local user ID, result/reason code, source IP, and correlation ID.
Do not log authorization codes, tokens, ticket/header bytes, client/proxy secrets,
nonces, PKCE verifiers, cookies, or full exception payloads.

## Deployment prerequisites

Before enabling SSO:

1. Assign a stable DNS name to TDeck and obtain a trusted TLS certificate.
2. Choose the identity path:
   - existing Entra/OIDC provider;
   - Keycloak bridging on-premises Kerberos to OIDC;
   - direct IIS or Apache Kerberos login.
3. For OIDC, register one exact callback and post-logout URI and create the
   client credential/certificate.
4. For direct Kerberos, register `HTTP/<tdeck-fqdn>@<REALM>` to exactly one
   service identity, configure browser/intranet policy, and verify DNS and clock
   synchronization.
5. Put the reverse proxy in front of TDeck, bind the TDeck backend privately,
   block its direct LAN port, and preserve TDeck's existing cache headers.
6. If direct Kerberos is expected on iPhones, deploy and test Apple's Kerberos
   SSO extension profile through MDM; otherwise use OIDC on iPhone.
7. Confirm there are at least two active Admins and at least one tested local
   break-glass account before switching from hybrid to SSO-required mode.

## Implementation phases

### Phase 0: deployment decision and lab

- Answer the review questions below.
- Stand up a non-production FQDN/TLS endpoint and test identity provider.
- Prove the target desktop and iPhone behavior before changing production auth.

### Phase 1: auth foundations

- Move new SSO code into a focused module such as `auth_sso.py`; do not expand
  hardware/control code paths.
- Add safe-return-path validation and use it for local and SSO login.
- Add external identity/session schema migrations and absolute session lifetime.
- Add secret-source handling, secure-cookie/external-URL validation, and an
  `auth preflight` CLI command.
- Add `local_login_enabled` and break-glass protections.

### Phase 2: OIDC

- Add the single selected, pinned OIDC library.
- Implement start/callback/logout with code flow, state, nonce, and PKCE S256.
- Map identities, create normal TDeck sessions, and implement pending access.
- Add the hybrid login UI and sanitized Activity Log events.

### Phase 3: administration

- Add pending identity review, explicit linking/unlinking, auth-source display,
  and local-login controls to Permissions/User Detail.
- Keep local TDeck groups as the only authorization source.

### Phase 4: optional direct Kerberos

- Add the proxy-proof completion endpoint and realm/principal validation.
- Provide separate hardened IIS and Apache example configurations.
- Add preflight checks that fail closed when the backend is publicly bound or
  proxy proof is absent.
- Verify Kerberos specifically and document any client that falls back or prompts.

### Phase 5: staged rollout

- Back up `auth.db` and configuration.
- Enable hybrid mode and pilot with non-admin accounts.
- Test session revocation, IdP outage, local recovery, desktop Kerberos, iPhone
  OIDC, and every existing permission boundary.
- Move to SSO-required only after a documented sign-off. Rollback changes the
  mode to `local`/`hybrid`; it does not require undoing the schema migration or
  deleting identity links.

## Test and acceptance plan

### Automated

- OIDC success plus wrong/missing/replayed state, nonce, issuer, audience, expiry,
  algorithm, signature, code, discovery metadata, and callback URL tests using a
  local fake provider and rotating test keys.
- Proxy assertion tests for absent/wrong secret, disallowed realm, malformed or
  duplicate identity headers, direct-client spoof attempts, locked/inactive users,
  and safe redirects.
- Identity collision/linking/JIT tests proving email and username never silently
  merge accounts.
- Authorization regression tests proving SSO users receive only their local
  TDeck groups and hardware allow-lists.
- Local login, lockout, force-password-change, session revoke, idle timeout,
  absolute timeout, CSRF, service-token, config transport, and Activity Log
  regression tests.
- Browser tests in Chromium, Firefox, WebKit, mobile Chromium, and mobile WebKit
  for login modes, errors, logout, and pending access.
- Dependency audit in CI and a test that secret/token field names never enter
  Config responses, exports, HTML, or logs.

### Deployment acceptance

- TDeck is reachable only at the HTTPS FQDN; the backend port is not reachable
  from another LAN host.
- OIDC login works with no TDeck password on desktop and iPhone.
- A captured/replayed callback and a forged proxy identity are rejected.
- Direct Kerberos, if enabled, obtains a ticket for the exact HTTP SPN; acceptance
  evidence shows that it did not silently use NTLM.
- An unknown identity gets no control permissions.
- Disabling/locking a TDeck user rejects new SSO login and revokes existing local
  sessions through the current session controls.
- IdP outage leaves the documented break-glass login usable.
- No credential, token, ticket, secret, or raw claims document appears in logs,
  browser storage, config export, or `auth.db`.

## Review decisions required before implementation

1. What identity system is available: Microsoft Entra ID, on-premises Active
   Directory only, Keycloak, or another OIDC provider?
2. Is the production TDeck process hosted directly on Windows, in Docker/Linux,
   or both?
3. Is zero-click Kerberos required on managed Windows desktops, or is an OIDC
   redirect acceptable everywhere?
4. Are the iPhones managed through MDM with network/VPN access to AD? If not,
   approve OIDC as the iPhone path.
5. What HTTPS FQDN will be assigned to TDeck?
6. Should unknown SSO identities be rejected outright or created as pending with
   no groups? The recommendation is pending/no-access for easier onboarding.
7. Is the first release single-provider only? The recommendation is yes.
8. What absolute session lifetime is acceptable? The recommendation is eight
   hours, with the existing 15-minute idle policy retained.
9. After rollout, should local login remain visible in hybrid mode or be limited
   to designated break-glass Admin accounts?

## Research references

- [IIS Windows Authentication](https://learn.microsoft.com/en-us/iis/configuration/system.webServer/security/authentication/windowsAuthentication/) documents the supported Kerberos/NTLM intranet model and provider configuration.
- [IIS Extended Protection](https://learn.microsoft.com/en-gb/iis/configuration/system.webserver/security/authentication/windowsauthentication/extendedprotection/) documents channel/service binding protections against relay attacks.
- [`mod_auth_gssapi`](https://github.com/gssapi/mod_auth_gssapi) is the maintained GSSAPI replacement for `mod_auth_kerb` and documents TLS-only, allowed-mechanism, session, and delegation controls.
- [Apple's Kerberos SSO extension](https://support.apple.com/guide/deployment/kerberos-sso-extension-depe6a1cda64/web) documents the MDM, AD-network, and HTTP 401 Negotiate requirements for iPhone/iPad/macOS.
- [Keycloak's administration guide](https://www.keycloak.org/docs/latest/server_admin/) documents its Kerberos bridge and OIDC client model.
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0-18.html) defines ID-token validation and the stable issuer/subject identity.
- [OAuth 2.0 Security Best Current Practice (RFC 9700)](https://www.rfc-editor.org/rfc/rfc9700.html) recommends exact redirects, code flow, transaction-bound state/nonce, and PKCE S256 for web clients.
- [Authlib Flask OIDC integration](https://docs.authlib.org/en/latest/oauth2/client/web/flask.html) documents the maintained Flask client flow.
- [Authlib releases and advisories](https://github.com/authlib/authlib/releases) are the basis for requiring a current exact pin and explicit algorithm restrictions.
- [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) is the Microsoft-maintained option if Entra ID is selected as the only provider.
- [OpenID Connect logout specifications](https://openid.net/the-openid-connect-logout-specifications-are-now-final-specifications/) define RP-initiated and provider-initiated logout options.
- [Flask-Kerberos on PyPI](https://pypi.org/project/Flask-Kerberos/) shows why that old beta extension is not selected.

