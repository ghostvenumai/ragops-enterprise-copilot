import { screen } from "@testing-library/react";
import { AnswerView } from "@/components/copilot/answer";
import type { QueryResponse } from "@/lib/api/schemas";
import { renderWithProviders } from "../support/render";

const base: QueryResponse = {
  answer: "Die Frist endet am 31.03.2026.",
  abstained: false,
  evidence_score: 0.82,
  citations: [{ source_id: "doc-1#0", title: "Mahnung RC", tenant_id: "tenant-alpha", score: 0.91, page_number: 2 }],
  metrics: { tokens: 120, estimated_cost_eur: 0, retrieval_latency_ms: 12.4, llm_latency_ms: 3.1, prompt_injection_detected: false },
  correlation_id: "corr-1",
};

describe("AnswerView", () => {
  it("renders model output as text, never as markup", () => {
    const hostile = { ...base, answer: '<img src=x onerror="alert(1)"><script>alert(2)</script>**bold**' };
    const { container } = renderWithProviders(<AnswerView result={hostile} replayed={false} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    // Markup stays visible text; only the **bold** structure becomes an element.
    expect(screen.getByTestId("answer-text").textContent).toBe('<img src=x onerror="alert(1)"><script>alert(2)</script>bold');
    expect(screen.getByTestId("answer-text").querySelector("strong")?.textContent).toBe("bold");
  });

  it("shows sources with page numbers and the query metrics", () => {
    renderWithProviders(<AnswerView result={base} replayed={false} />);
    expect(screen.getByText("Mahnung RC")).toBeInTheDocument();
    expect(screen.getByTestId("citation-page")).toHaveTextContent("Seite 2");
    expect(screen.getByTestId("evidence-score")).toHaveTextContent("82");
    expect(screen.getByTestId("query-metrics")).toHaveTextContent("corr-1");
  });

  it("makes abstention, weak evidence and replays explicit", () => {
    const first = renderWithProviders(<AnswerView result={{ ...base, abstained: true, citations: [] }} replayed />);
    expect(screen.getByText("Keine belastbare Antwort")).toBeInTheDocument();
    expect(screen.getByTestId("replayed")).toBeInTheDocument();
    first.unmount();
    renderWithProviders(<AnswerView result={{ ...base, evidence_score: 0.2 }} replayed={false} />);
    expect(screen.getByText("Geringe Belegstärke")).toBeInTheDocument();
  });

  it("renders bullet lists and bold spans as elements without parsing HTML", () => {
    const structured = { ...base, answer: "**Ergebnis**\n- erste [doc-1#0]\n- zweite <b>x</b>\n\nSchluss" };
    renderWithProviders(<AnswerView result={structured} replayed={false} />);
    const answer = screen.getByTestId("answer-text");
    expect(answer.querySelectorAll("li")).toHaveLength(2);
    expect(answer.querySelector("b")).toBeNull();
    expect(answer.textContent).toContain("zweite <b>x</b>");
  });

  it("is localized in English without translating the answer itself", () => {
    renderWithProviders(<AnswerView result={base} replayed={false} />, { locale: "en-US" });
    expect(screen.getByTestId("citation-page")).toHaveTextContent("Page 2");
    expect(screen.getByTestId("answer-text")).toHaveTextContent("Die Frist endet am 31.03.2026.");
  });
});
