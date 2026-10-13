import { fireEvent, screen, waitFor } from "@testing-library/react";
import { ReviewDialog, type ReviewTarget } from "@/components/intelligence/review-dialog";
import { renderWithProviders, stubFetch } from "../support/render";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const fact: ReviewTarget = { entityType: "fact", entityId: "f-1", expectedVersion: 3, label: "Restbetrag", category: "amount", currency: "EUR" };

afterEach(() => vi.unstubAllGlobals());

describe("ReviewDialog", () => {
  it("sends the expected version and a typed correction, with the CSRF token", async () => {
    const calls = stubFetch({ "POST /api/bff/v1/intelligence/reviews": () => ({ body: { id: "f-1", review_version: 4 } }) });
    const onClose = vi.fn();
    renderWithProviders(<ReviewDialog target={fact} onClose={onClose} />);
    fireEvent.change(screen.getByTestId("review-action"), { target: { value: "correct" } });
    fireEvent.change(screen.getByTestId("review-amount"), { target: { value: "1234,50" } });
    fireEvent.click(screen.getByTestId("review-submit"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const body = JSON.parse(String(calls[0]?.init?.body));
    expect(body).toEqual({
      entity_type: "fact",
      entity_id: "f-1",
      action: "correct",
      expected_version: 3,
      reason_category: "verified_against_source",
      value: { amount: "1234.50", currency: "EUR" },
    });
    expect(new Headers(calls[0]?.init?.headers).get("x-csrf-token")).toBe("csrf-test");
  });

  it("refuses an invalid amount locally", async () => {
    const calls = stubFetch({});
    renderWithProviders(<ReviewDialog target={fact} onClose={vi.fn()} />);
    fireEvent.change(screen.getByTestId("review-action"), { target: { value: "correct" } });
    fireEvent.change(screen.getByTestId("review-amount"), { target: { value: "-5" } });
    fireEvent.click(screen.getByTestId("review-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("nicht negativen Betrag");
    expect(calls).toHaveLength(0);
  });

  it("explains a 409 version conflict and blocks a blind resubmit", async () => {
    stubFetch({ "POST /api/bff/v1/intelligence/reviews": () => ({ status: 409, body: { error: "conflict", detail: "conflict" } }) });
    renderWithProviders(<ReviewDialog target={fact} onClose={vi.fn()} />);
    fireEvent.click(screen.getByTestId("review-submit"));
    expect(await screen.findByText("Zwischenzeitlich geändert")).toBeInTheDocument();
    expect(screen.getByTestId("review-submit")).toBeDisabled();
  });

  it("offers only the actions the backend accepts for the entity", () => {
    renderWithProviders(<ReviewDialog target={{ ...fact, entityType: "claim_group", category: undefined }} onClose={vi.fn()} />);
    const options = [...(screen.getByTestId("review-action") as HTMLSelectElement).options].map((option) => option.value);
    expect(options).toEqual(["confirm", "dismiss"]);
  });
});
