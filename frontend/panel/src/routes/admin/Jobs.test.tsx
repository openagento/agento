import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Jobs } from "./Jobs";
import { renderAt, stubApi, teardown } from "./testing";

const row = (id: number, status: string, ref: string) => ({
  id, type: "cron", status, source: "jira", reference_id: ref, agent_type: "claude", agent_view_code: "av",
  created_at: "2026-10-02T10:00:00Z", started_at: "2026-10-02T10:00:00Z", finished_at: "2026-10-02T10:00:03Z",
  input_tokens: 1, output_tokens: 2, error_class: null,
});
afterEach(teardown);

describe("Jobs", () => {
  it("sends the status filter from the URL and searches the rows on the client", async () => {
    const api = await stubApi({ "/api/admin/jobs": [row(1, "FAILED", "AG-1"), row(2, "FAILED", "AG-2")] });
    renderAt("/admin/jobs?status=FAILED", <Jobs />);
    expect(await screen.findByText("AG-2")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/jobs")[0].url.search).toBe("?status=FAILED");
    fireEvent.change(screen.getByRole("textbox", { name: "Search jobs" }), { target: { value: "ag-1" } });
    expect(screen.queryByText("AG-2")).toBeNull();
    expect(screen.getByText("AG-1")).toBeInTheDocument();
    expect(screen.getByText("3s")).toBeInTheDocument();
  });

  it("All sends no status", async () => {
    const api = await stubApi({ "/api/admin/jobs": [] });
    renderAt("/admin/jobs", <Jobs />);
    expect(await screen.findByText("No jobs")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/jobs")[0].url.search).toBe("");
  });

  it("Details opens a drawer with prompt, output and the copyable replay command", async () => {
    await stubApi({
      "/api/admin/jobs": [row(7, "FAILED", "AG-7")],
      "/api/admin/jobs/7": { ...row(7, "FAILED", "AG-7"), model: "m", error_message: "boom", result_summary: null,
        prompt: "do it", output: "done" },
    });
    renderAt("/admin/jobs", <Jobs />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByText("do it")).toBeInTheDocument();
    expect(within(drawer).getByText("bin/agento replay 7")).toBeInTheDocument();
    expect(within(drawer).getByText(/Runs in the cron container/)).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });
});
