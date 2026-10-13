import { fireEvent, screen, waitFor } from "@testing-library/react";
import { JobsTable } from "@/components/knowledge/jobs-table";
import { renderWithProviders, stubFetch } from "../support/render";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
afterEach(() => vi.unstubAllGlobals());

const job = (status: string) => ({
  job_id: "11111111-1111-1111-1111-111111111111",
  status,
  stage: null,
  progress: null,
  document_id: "22222222-2222-2222-2222-222222222222",
  version_id: "33333333-3333-3333-3333-333333333333",
  error_code: status === "failed" ? "queue_unavailable" : null,
  title: "Ausfall",
  filename: "a.pdf",
  review_required: false,
  pages_total: 0,
  pages_review: 0,
});

describe("JobsTable", () => {
  it("keeps the retry acknowledgement visible after the list refreshes into polling", async () => {
    let listing = 0;
    stubFetch({
      "GET /api/bff/v1/ingestion/jobs": () => ({ body: [job(++listing === 1 ? "failed" : listing === 2 ? "queued" : "completed")] }),
      "POST /api/bff/v1/ingestion/jobs/11111111-1111-1111-1111-111111111111/retry": () => ({ status: 202, body: { job_id: "11111111-1111-1111-1111-111111111111", status: "queued" } }),
    });
    renderWithProviders(<JobsTable />);
    fireEvent.click(await screen.findByTestId("job-retry"));
    expect(await screen.findByText("Auftrag erneut eingereiht")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("job-status")).toHaveAttribute("data-state", "queued"));
    expect(screen.getByText("Auftrag erneut eingereiht")).toBeInTheDocument();
  });

  it("hides retry for roles without ingestion management", async () => {
    stubFetch({ "GET /api/bff/v1/ingestion/jobs": () => ({ body: [job("failed")] }) });
    renderWithProviders(<JobsTable />, { session: { roles: ["viewer"] } });
    expect(await screen.findByTestId("job-status")).toHaveAttribute("data-state", "failed");
    expect(screen.queryByTestId("job-retry")).toBeNull();
  });
});
