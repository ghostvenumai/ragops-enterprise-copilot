import "server-only";
import { randomToken, sha256 } from "./crypto";

export const SESSION_COOKIE = "__Host-ragops-session";
export const LOGIN_COOKIE = "__Host-ragops-login";

export interface SessionUser {
  subject: string;
  name: string | null;
  email: string | null;
  tenantId: string;
  roles: string[];
}

export interface SessionRecord {
  user: SessionUser;
  accessToken: string;
  accessExpiresAt: number;
  refreshToken: string | null;
  refreshExpiresAt: number | null;
  idToken: string | null;
  csrfToken: string;
  createdAt: number;
  lastSeenAt: number;
}

export interface SessionLimits {
  idleMs: number;
  absoluteMs: number;
}

const MAX_SESSIONS = 5000;

/**
 * Server-side session store. The browser only holds an opaque random identifier;
 * tokens never leave this process. Entries are keyed by a hash of the identifier.
 * Single-instance by design: a horizontally scaled deployment needs a shared store
 * (documented in docs/WEB_FRONTEND.md).
 */
export class MemorySessionStore {
  private readonly entries = new Map<string, SessionRecord>();
  private readonly refreshing = new Map<string, Promise<SessionRecord | null>>();

  constructor(private readonly now: () => number = Date.now) {}

  create(record: Omit<SessionRecord, "csrfToken" | "createdAt" | "lastSeenAt">): {
    id: string;
    session: SessionRecord;
  } {
    this.sweep(Number.POSITIVE_INFINITY, Number.POSITIVE_INFINITY);
    if (this.entries.size >= MAX_SESSIONS) {
      // Drop the oldest entry instead of growing without bound.
      const oldest = this.entries.keys().next().value;
      if (oldest !== undefined) this.entries.delete(oldest);
    }
    const id = randomToken(32);
    const at = this.now();
    const session: SessionRecord = { ...record, csrfToken: randomToken(32), createdAt: at, lastSeenAt: at };
    this.entries.set(sha256(id), session);
    return { id, session };
  }

  get(id: string | undefined, limits: SessionLimits): SessionRecord | null {
    if (!id || id.length > 128) return null;
    const key = sha256(id);
    const session = this.entries.get(key);
    if (!session) return null;
    const at = this.now();
    if (at - session.lastSeenAt > limits.idleMs || at - session.createdAt > limits.absoluteMs) {
      this.entries.delete(key);
      return null;
    }
    session.lastSeenAt = at;
    return session;
  }

  update(id: string, changes: Partial<SessionRecord>): void {
    const key = sha256(id);
    const session = this.entries.get(key);
    if (session) this.entries.set(key, { ...session, ...changes });
  }

  delete(id: string | undefined): SessionRecord | null {
    if (!id) return null;
    const key = sha256(id);
    const session = this.entries.get(key) ?? null;
    this.entries.delete(key);
    this.refreshing.delete(key);
    return session;
  }

  /** Runs one refresh per session at a time; concurrent callers share the result. */
  refreshOnce(id: string, run: () => Promise<SessionRecord | null>): Promise<SessionRecord | null> {
    const key = sha256(id);
    const pending = this.refreshing.get(key);
    if (pending) return pending;
    const promise = run().finally(() => this.refreshing.delete(key));
    this.refreshing.set(key, promise);
    return promise;
  }

  sweep(idleMs: number, absoluteMs: number): void {
    const at = this.now();
    for (const [key, session] of this.entries) {
      if (at - session.lastSeenAt > idleMs || at - session.createdAt > absoluteMs) {
        this.entries.delete(key);
      }
    }
  }

  get size(): number {
    return this.entries.size;
  }
}

const STORE_KEY = Symbol.for("ragops.web.sessionStore");

/** One store per process, shared by every route bundle. */
export function sessionStore(): MemorySessionStore {
  const holder = globalThis as unknown as Record<symbol, MemorySessionStore | undefined>;
  holder[STORE_KEY] ??= new MemorySessionStore();
  return holder[STORE_KEY];
}
