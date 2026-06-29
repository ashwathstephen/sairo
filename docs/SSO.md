# Single Sign-On (SSO)

Sairo supports three ways to sign users in through your own identity provider:

- **OIDC** (OpenID Connect) — a generic client that works with any compliant provider (Keycloak, Authentik, Okta, Auth0, Entra ID, Google, Dex, Zitadel, …)
- **OAuth** — built-in Google / GitHub
- **LDAP** — Active Directory and other LDAP directories

This guide focuses on **OIDC**, the recommended path for most providers.

## The model: identity in, access assigned

By design, **OIDC syncs the username only**. A user signing in for the first time is created as a **viewer with no bucket access**. An admin then grants per-bucket Read/Write under **Users → Manage access**. This keeps your identity provider as the source of *who someone is*, while Sairo stays the source of *what they can touch*. (Optional group→role mapping is available — see [Group mapping](#optional-grouprole-mapping) — but it's off by default.)

## Enable it

OIDC turns on when `OIDC_ISSUER` and `OIDC_CLIENT_ID` are both set. Endpoints are auto-discovered from `<issuer>/.well-known/openid-configuration`, and every ID token is fully validated (JWKS signature, `iss`/`aud`/`exp`, nonce) before any claim is trusted.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OIDC_ISSUER` | ✅ | — | e.g. `https://login.example.com/realms/main` |
| `OIDC_CLIENT_ID` | ✅ | — | the client/app id from your provider |
| `OIDC_CLIENT_SECRET` | — | — | omit for a **public** client (PKCE is always used) |
| `OIDC_PROVIDER_NAME` | — | `SSO` | label on the login button |
| `OIDC_USERNAME_CLAIM` | — | `preferred_username` | claim used as the local username |
| `OIDC_SCOPES` | — | `openid profile email` | |
| `OIDC_DEFAULT_ROLE` | — | `viewer` | role for new users |
| `OIDC_ALLOWED_DOMAINS` | — | — | comma-separated email-domain allowlist |
| `OIDC_ADMIN_GROUP` | — | — | members become admins (enables group mapping) |
| `OIDC_GROUPS_CLAIM` | — | `groups` | claim that carries the user's groups |
| `OIDC_REQUIRE_VERIFIED_EMAIL` | — | `false` | reject logins whose email isn't verified |
| `OIDC_RP_LOGOUT` | — | `false` | also end the IdP session on logout (single logout) |

**Redirect URI to register with your provider:**
```
https://<your-sairo-host>/api/auth/oidc/callback
```
Use `http://localhost:8000/api/auth/oidc/callback` for local testing. Most providers require **HTTPS** redirect URIs in production (`http://localhost` is the common exception).

## Reusing an existing identity provider

You don't need a new IdP — point Sairo at one you already run, and **register a new client** for Sairo on it. Your users, passwords, MFA, and SSO sessions are reused; only the client registration is new. Reusing another application's *exact* client is possible (add Sairo's redirect URI to it, ensure it emits the claims you need) but not recommended — a dedicated client per app is cleaner and safer.

## Per-provider setup

> Validation status as of this release: **Keycloak, Authentik, and Dex are tested end-to-end** (live, in a browser). **Google** is validated against its real discovery + JWKS; **Okta / Auth0 / Entra ID** are validated at the claim-shape level. All are standards-compliant and use the same generic client.

### Keycloak
Create a client (`Clients → Create`): client type **OpenID Connect**, valid redirect URI `https://<host>/api/auth/oidc/callback`, and (for a confidential client) copy the secret from the **Credentials** tab.
```
OIDC_ISSUER=https://<keycloak>/realms/<realm>
OIDC_CLIENT_ID=sairo
OIDC_CLIENT_SECRET=<from Credentials tab>
OIDC_PROVIDER_NAME=Keycloak
```
For group mapping, add a **Group Membership** mapper (claim name `groups`) to the client and set `OIDC_ADMIN_GROUP`.

### Authentik
`Applications → Providers → Create → OAuth2/OpenID Provider`. Set the redirect URI, pick a signing key, then create an Application bound to that provider.
```
OIDC_ISSUER=https://<authentik>/application/o/<app-slug>/
OIDC_CLIENT_ID=<provider client id>
OIDC_CLIENT_SECRET=<provider client secret>
OIDC_PROVIDER_NAME=Authentik
```

### Okta
`Applications → Create App Integration → OIDC → Web Application`. Add the sign-in redirect URI.
```
OIDC_ISSUER=https://<your-org>.okta.com         # or https://<org>.okta.com/oauth2/<authz-server>
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
OIDC_PROVIDER_NAME=Okta
```
Okta often delivers groups via **userinfo** — Sairo fetches it automatically. Add a "groups" claim to enable group mapping.

### Auth0
Create a **Regular Web Application**, add the callback URL.
```
OIDC_ISSUER=https://<tenant>.us.auth0.com/
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
OIDC_USERNAME_CLAIM=nickname             # Auth0 has no preferred_username
OIDC_PROVIDER_NAME=Auth0
```
Auth0 custom/group claims are usually **namespaced** (e.g. `https://your-app/groups`) — set `OIDC_GROUPS_CLAIM` to that full URL if you use group mapping.

### Microsoft Entra ID (Azure AD)
Register an app, add a Web redirect URI, create a client secret.
```
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_CLIENT_ID=<application (client) id>
OIDC_CLIENT_SECRET=<client secret>
OIDC_PROVIDER_NAME=Microsoft
```
Entra emits groups as **object IDs (GUIDs)** — set `OIDC_ADMIN_GROUP` to the admin group's GUID. Add a groups claim to the token configuration.

### Google
Create an **OAuth client ID** (type: Web application) in the Google Cloud Console and add the redirect URI.
```
OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
OIDC_USERNAME_CLAIM=email                # Google has no username concept
OIDC_SCOPES=openid email profile
OIDC_PROVIDER_NAME=Google
# optional — lock to a Workspace domain:
OIDC_ALLOWED_DOMAINS=yourcompany.com
```
Plain Google OIDC does not emit groups, so group→role mapping doesn't apply — use the default username-only model and assign access in Sairo.

### Dex
Add a `staticClient` with the redirect URI in your Dex config.
```
OIDC_ISSUER=https://<dex-host>/dex
OIDC_CLIENT_ID=sairo
OIDC_CLIENT_SECRET=<staticClient secret>
OIDC_USERNAME_CLAIM=name
OIDC_PROVIDER_NAME=Dex
```

## Optional: group→role mapping

Off by default (username-only). When you set `OIDC_ADMIN_GROUP`, members of that group become **admins** and everyone else gets `OIDC_DEFAULT_ROLE` — re-evaluated on **every** login, so removing someone from the group demotes them next time. Matching is on the whole group value or a delimited component (path segment / DN `cn=`), never a loose substring, so `sairo-admins` will **not** match `sairo-admins-readonly`.

## Optional: single logout

With `OIDC_RP_LOGOUT=true`, logging out of Sairo also ends the session at your identity provider (RP-initiated logout via the provider's `end_session_endpoint`).

## Helm

The chart exposes all of the above under `oidc:` in `values.yaml` (the client secret is stored in the release Secret). See [`charts/sairo/values.yaml`](../charts/sairo/values.yaml).

## Troubleshooting

If sign-in fails, the login page shows a friendly message and the URL carries an `?error=` code:

| Code | Meaning / fix |
|---|---|
| `account_conflict` | The username already exists with a different sign-in method (e.g. a local account). Reconcile the account; SSO won't take over an account from another source. |
| `oidc_invalid_token` | The ID token failed validation (signature/`iss`/`aud`/`azp`). Check the issuer + client id. |
| `oidc_state_mismatch` / `oidc_nonce_mismatch` | The sign-in session expired or cookies were dropped. Retry; ensure cookies aren't blocked. |
| `oidc_no_username` | The provider didn't return a username claim — set `OIDC_USERNAME_CLAIM` to one it does send (e.g. `email`). |
| `email_not_verified` | `OIDC_REQUIRE_VERIFIED_EMAIL=true` and the provider reports the email unverified. |
| `domain_not_allowed` | The email domain isn't in `OIDC_ALLOWED_DOMAINS`. |
