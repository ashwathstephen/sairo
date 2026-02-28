import * as fs from 'fs';
import * as path from 'path';

/**
 * HTTP API client for test setup/teardown.
 * Bypasses the browser to seed data via Sairo REST API.
 */
export class ApiClient {
  private baseURL: string;
  private cookies: string = '';

  constructor(baseURL: string) {
    this.baseURL = baseURL;
  }

  /** Poll /healthz until the app is ready. */
  async waitForHealthy(timeoutMs: number): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        const res = await fetch(`${this.baseURL}/healthz`);
        if (res.ok) return;
      } catch {}
      await new Promise(r => setTimeout(r, 1000));
    }
    throw new Error(`Sairo not healthy after ${timeoutMs}ms`);
  }

  /** Login and store session cookie. */
  async login(username: string, password: string): Promise<void> {
    const res = await fetch(`${this.baseURL}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      redirect: 'manual',
    });
    if (!res.ok) throw new Error(`Login failed: ${res.status} ${await res.text()}`);
    const setCookie = res.headers.get('set-cookie');
    if (setCookie) this.cookies = setCookie.split(';')[0];
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return { ...(this.cookies ? { Cookie: this.cookies } : {}), ...extra };
  }

  async createBucket(name: string): Promise<void> {
    const res = await fetch(`${this.baseURL}/api/buckets`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ name }),
    });
    // Ignore 409 (already exists)
    if (!res.ok && res.status !== 409) {
      throw new Error(`Create bucket ${name} failed: ${res.status}`);
    }
  }

  async deleteBucket(name: string): Promise<void> {
    await fetch(`${this.baseURL}/api/buckets/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: this.headers(),
    });
  }

  async emptyBucket(bucket: string): Promise<void> {
    // List all objects and delete them
    const res = await fetch(`${this.baseURL}/api/b/${bucket}/list?prefix=`, {
      headers: this.headers(),
    });
    if (!res.ok) return;
    const text = await res.text();
    const keys: string[] = [];
    const folderPrefixes: string[] = [];

    for (const line of text.split('\n')) {
      if (!line.trim()) continue;
      try {
        const page = JSON.parse(line);
        if (page.files) keys.push(...page.files.map((f: any) => f.key));
        if (page.folders) folderPrefixes.push(...page.folders.map((f: any) => f.prefix));
      } catch {}
    }

    // Delete files
    if (keys.length > 0) {
      await fetch(`${this.baseURL}/api/b/${bucket}/objects`, {
        method: 'DELETE',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ keys }),
      });
    }

    // Delete folders
    for (const pfx of folderPrefixes) {
      await fetch(`${this.baseURL}/api/b/${bucket}/folder`, {
        method: 'DELETE',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ prefix: pfx }),
      });
    }
  }

  async uploadFile(bucket: string, prefix: string, filename: string): Promise<void> {
    const filePath = path.resolve(__dirname, '..', 'test-data', filename);
    const fileData = fs.readFileSync(filePath);

    // Build multipart form data manually
    const boundary = '----E2ETestBoundary' + Date.now();
    const parts: Buffer[] = [];

    // Prefix field
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="prefix"\r\n\r\n${prefix}\r\n`
    ));

    // File field
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="${filename}"\r\nContent-Type: application/octet-stream\r\n\r\n`
    ));
    parts.push(fileData);
    parts.push(Buffer.from('\r\n'));

    // End boundary
    parts.push(Buffer.from(`--${boundary}--\r\n`));

    const body = Buffer.concat(parts);

    const res = await fetch(`${this.baseURL}/api/b/${bucket}/upload`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': `multipart/form-data; boundary=${boundary}` }),
      body: body,
    });
    if (!res.ok) {
      throw new Error(`Upload ${filename} to ${bucket}/${prefix} failed: ${res.status} ${await res.text()}`);
    }
  }

  async enableVersioning(bucket: string): Promise<void> {
    await fetch(`${this.baseURL}/api/b/${bucket}/versioning?enabled=true`, {
      method: 'PUT',
      headers: this.headers(),
    });
  }

  async waitForIndex(bucket: string, timeoutMs: number): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        const res = await fetch(`${this.baseURL}/api/b/${bucket}/crawl-status`, {
          headers: this.headers(),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.status === 'complete' || data.status === 'ready') return;
        }
      } catch {}
      await new Promise(r => setTimeout(r, 2000));
    }
    // Don't throw — indexing is best-effort for setup
    console.warn(`Index for ${bucket} not ready after ${timeoutMs}ms`);
  }

  async createUser(username: string, password: string, role: string): Promise<void> {
    const res = await fetch(`${this.baseURL}/api/auth/users`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ username, password, role }),
    });
    if (!res.ok && res.status !== 409) {
      throw new Error(`Create user ${username} failed: ${res.status}`);
    }
  }

  async deleteUser(username: string): Promise<void> {
    await fetch(`${this.baseURL}/api/auth/users/${encodeURIComponent(username)}`, {
      method: 'DELETE',
      headers: this.headers(),
    });
  }

  async createShareLink(bucket: string, key: string, expiresHours: number, password?: string): Promise<string> {
    const res = await fetch(`${this.baseURL}/api/share-links`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        bucket,
        key,
        expires_hours: expiresHours,
        ...(password ? { password } : {}),
      }),
    });
    if (!res.ok) throw new Error(`Create share link failed: ${res.status}`);
    const data = await res.json();
    return data.token;
  }

  async triggerCrawl(bucket: string): Promise<void> {
    await fetch(`${this.baseURL}/api/b/${bucket}/crawl`, {
      method: 'POST',
      headers: this.headers(),
    });
  }
}
