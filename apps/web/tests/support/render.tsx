import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type * as React from "react";
import { SessionProvider, type ClientSession } from "@/components/providers/session-context";
import type { Locale } from "@/i18n/config";
import { messagesFor } from "@/i18n/messages";

export const SESSION: ClientSession = { name: "RC Admin", email: null, tenantId: "tenant-alpha", roles: ["admin"], csrfToken: "csrf-test" };

export function renderWithProviders(ui: React.ReactElement, options: { locale?: Locale; session?: Partial<ClientSession> } = {}) {
  const locale = options.locale ?? "de-DE";
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <NextIntlClientProvider locale={locale} messages={messagesFor(locale)} timeZone="Europe/Berlin">
      <SessionProvider value={{ ...SESSION, ...options.session }}>
        <QueryClientProvider client={client}>{ui}</QueryClientProvider>
      </SessionProvider>
    </NextIntlClientProvider>,
  );
}

/** A fetch stub that records requests and answers from a route table. */
export function stubFetch(routes: Record<string, (init?: RequestInit) => { status?: number; body: unknown; headers?: Record<string, string> }>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    calls.push({ url, init });
    const key = `${init?.method ?? "GET"} ${url}`;
    const handler = routes[key] ?? routes[url];
    if (!handler) return new Response(JSON.stringify({ error: "not_found" }), { status: 404 });
    const reply = handler(init);
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { "content-type": "application/json", ...reply.headers } });
  });
  vi.stubGlobal("fetch", fetchStub);
  return calls;
}
