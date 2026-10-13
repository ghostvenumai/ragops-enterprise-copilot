import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Copilot } from "@/components/copilot/copilot";
import { renderWithProviders, stubFetch } from "../support/render";

beforeAll(() => {
  // Current Chromium returns a Promise from scrollIntoView; an effect that leaks it crashes
  // React 19 on the next turn ("u is not a function"), which this stub reproduces.
  Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve()) as unknown as typeof Element.prototype.scrollIntoView;
});
afterEach(() => vi.unstubAllGlobals());

const answer = {
  answer: "Antwort",
  abstained: false,
  evidence_score: 0.9,
  citations: [],
  metrics: { tokens: 1, estimated_cost_eur: 0, retrieval_latency_ms: 1, llm_latency_ms: 1, prompt_injection_detected: false },
  correlation_id: "c",
};

describe("Copilot", () => {
  it("uses one idempotency key per question and reuses it when the same request is resent", async () => {
    let attempt = 0;
    const calls = stubFetch({
      "POST /api/bff/v1/query": () => (++attempt === 1 ? { status: 429, body: { error: "rate_limited" }, headers: { "retry-after": "3" } } : { body: answer }),
    });
    renderWithProviders(<Copilot />);
    fireEvent.change(screen.getByTestId("question"), { target: { value: "Welche Frist gilt?" } });
    fireEvent.click(screen.getByTestId("ask"));
    expect(await screen.findByTestId("refusal-rate_limited")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("retry-query"));
    await waitFor(() => expect(screen.getByTestId("answer-text")).toHaveTextContent("Antwort"));
    const keys = calls.map((call) => new Headers(call.init?.headers).get("idempotency-key"));
    expect(keys[0]).toMatch(/^ui-/);
    expect(keys[1]).toBe(keys[0]);
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ question: "Welche Frist gilt?", top_k: 3 });
  });

  it("gives a new question a new key", async () => {
    const calls = stubFetch({ "POST /api/bff/v1/query": () => ({ body: answer }) });
    renderWithProviders(<Copilot />);
    for (const question of ["Erste Frage", "Zweite Frage"]) {
      fireEvent.change(screen.getByTestId("question"), { target: { value: question } });
      fireEvent.click(screen.getByTestId("ask"));
      await waitFor(() => expect(screen.queryByTestId("answer-pending")).toBeNull());
    }
    const keys = calls.map((call) => new Headers(call.init?.headers).get("idempotency-key"));
    expect(new Set(keys).size).toBe(2);
  });

  it("validates empty questions without calling the API", () => {
    const calls = stubFetch({});
    renderWithProviders(<Copilot />);
    fireEvent.click(screen.getByTestId("ask"));
    expect(screen.getByRole("alert")).toHaveTextContent("Bitte geben Sie eine Frage ein.");
    expect(calls).toHaveLength(0);
  });

  it.each([
    [409, "no eligible model", "refusal-no_model", false],
    [409, "idempotency key reused with a different request", "refusal-key_conflict", false],
    [409, "idempotent request in progress", "refusal-in_progress", true],
    [429, "budget_limit_exceeded", "refusal-budget_exceeded", false],
    [503, "accounting not configured", "refusal-not_configured", false],
  ])("explains %s %s as a controlled refusal", async (status, detail, testId, retry) => {
    stubFetch({ "POST /api/bff/v1/query": () => ({ status, body: { error: "x", detail } }) });
    renderWithProviders(<Copilot />);
    fireEvent.change(screen.getByTestId("question"), { target: { value: "Frage" } });
    fireEvent.click(screen.getByTestId("ask"));
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
    expect(screen.queryByTestId("retry-query") !== null).toBe(retry);
  });

  it("shows a controlled refusal for forbidden requests without a retry button", async () => {
    stubFetch({ "POST /api/bff/v1/query": () => ({ status: 403, body: { error: "forbidden" } }) });
    renderWithProviders(<Copilot />, { locale: "en-US" });
    fireEvent.change(screen.getByTestId("question"), { target: { value: "x" } });
    fireEvent.click(screen.getByTestId("ask"));
    expect(await screen.findByTestId("refusal-forbidden")).toHaveTextContent("You are not permitted");
    expect(screen.queryByTestId("retry-query")).toBeNull();
  });
});
