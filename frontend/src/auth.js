const BASE = "/api/auth";

export async function checkAuth() {
  try {
    const res = await fetch(`${BASE}/me`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function login(username, password) {
  const res = await fetch(`${BASE}/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Login failed");
  }
  const data = await res.json();
  if (data.requires_2fa) {
    const e = new Error("2FA required");
    e.requires_2fa = true;
    e.username = data.username;
    throw e;
  }
  return data;
}

export async function logout() {
  // Returns an SSO end-session URL when the session is an OIDC one and
  // RP-initiated logout is enabled, so the caller can complete a single logout.
  try {
    const res = await fetch(`${BASE}/logout`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    return data.sso_logout_url || null;
  } catch {
    return null;
  }
}

export async function loginS3(accessKey, secretKey) {
  const res = await fetch(`${BASE}/login-s3`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_key: accessKey, secret_key: secretKey }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Invalid S3 credentials");
  }
  return res.json();
}

export async function loginLdap(username, password) {
  const res = await fetch(`${BASE}/ldap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "LDAP login failed");
  }
  const data = await res.json();
  if (data.requires_2fa) {
    const e = new Error("2FA required");
    e.requires_2fa = true;
    e.username = data.username;
    throw e;
  }
  return data;
}

export async function refreshSession() {
  try {
    const res = await fetch(`${BASE}/refresh`, { method: "POST" });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
