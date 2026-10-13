import type { z } from "zod";
import { apiErrorSchema } from "./schemas";

/** Error as the UI sees it: status, stable code, optional generic detail, Retry-After. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail?: string,
    readonly retryAfterSeconds?: number,
  ) {
    super(code);
  }
}

/** The backend answered with a shape this UI does not understand; never render it. */
export class ContractError extends Error {
  constructor(readonly path: string) {
    super("contract_violation");
  }
}

export interface RequestOptions {
  csrfToken?: string;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

const BFF = "/api/bff";

function retryAfter(response: Response): number | undefined {
  const value = Number(response.headers.get("retry-after"));
  return Number.isFinite(value) && value > 0 ? Math.min(value, 120) : undefined;
}

async function failure(response: Response): Promise<ApiError> {
  const parsed = apiErrorSchema.safeParse(await response.json().catch(() => null));
  const code = parsed.success ? parsed.data.error : "upstream_error";
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("ragops:session-expired"));
  }
  return new ApiError(response.status, code, parsed.success ? parsed.data.detail : undefined, retryAfter(response));
}

export interface ApiResult<T> {
  data: T;
  replayed: boolean;
  status: number;
}

async function send<T>(
  method: string,
  path: string,
  schema: z.ZodType<T>,
  body: BodyInit | undefined,
  options: RequestOptions,
  contentType?: string,
  allowStatus?: number,
): Promise<ApiResult<T>> {
  const headers = new Headers({ accept: "application/json" });
  if (contentType) headers.set("content-type", contentType);
  if (method !== "GET") {
    if (!options.csrfToken) throw new ApiError(403, "csrf_missing");
    headers.set("x-csrf-token", options.csrfToken);
  }
  if (options.idempotencyKey) headers.set("idempotency-key", options.idempotencyKey);
  let response: Response;
  try {
    response = await fetch(`${BFF}${path}`, {
      method,
      headers,
      body,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error");
  }
  if (!response.ok && response.status !== allowStatus) throw await failure(response);
  const parsed = schema.safeParse(await response.json().catch(() => undefined));
  if (!parsed.success) throw new ContractError(path);
  return { data: parsed.data, replayed: response.headers.get("idempotency-replayed") === "true", status: response.status };
}

export const api = {
  get<T>(path: string, schema: z.ZodType<T>, options: RequestOptions & { allowStatus?: number } = {}) {
    return send("GET", path, schema, undefined, options, undefined, options.allowStatus).then((result) => result.data);
  },
  getResult<T>(path: string, schema: z.ZodType<T>, options: RequestOptions & { allowStatus?: number } = {}) {
    return send("GET", path, schema, undefined, options, undefined, options.allowStatus);
  },
  post<T>(path: string, payload: unknown, schema: z.ZodType<T>, options: RequestOptions = {}) {
    const body = payload === undefined ? undefined : JSON.stringify(payload);
    return send("POST", path, schema, body, options, body ? "application/json" : undefined);
  },
  upload<T>(path: string, form: FormData, schema: z.ZodType<T>, options: RequestOptions = {}) {
    return send("POST", path, schema, form, options);
  },
  async text(path: string, options: RequestOptions = {}): Promise<string> {
    const response = await fetch(`${BFF}${path}`, { cache: "no-store", credentials: "same-origin", signal: options.signal });
    if (!response.ok) throw await failure(response);
    return response.text();
  },
};

export function newIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}
