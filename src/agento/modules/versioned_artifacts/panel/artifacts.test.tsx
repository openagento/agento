import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { renderAt, renderInLocale, stubApi, teardown } from "../../../../../frontend/panel/src/routes/admin/testing";
import ArtifactsPage from "./ArtifactsPage";
import ArtifactPage from "./ArtifactPage";

const V1 = "v-20261010-090200-77cd";
const V2 = "v-20261011-101500-ab12";
const LIST = [{ artifact_code: "approver", title: "Jira approver", current_version: V2, auth_enabled: true, open_drafts: [{ draft_id: "d1" }] }];
const ev = (operation: string, description: string | null) =>
  ({ operation, job_id: 812, agent_view_id: 3, actor: "agent", description, created_at: "2026-10-11T10:15:00Z" });
const VERSIONS = [
  { version_id: V2, preview_path: null, events: [ev("save_version", "Adds CSV export")] },
  { version_id: V1, preview_path: null, events: [ev("save_version", "First version"), ev("miniapp.approve", "actions: x")] },
];
const BASE = "/api/versioned_artifacts/artifacts";

afterEach(teardown);

describe("the artifacts list", () => {
  it("lists every artifact with its current version, share state and open drafts", async () => {
    await stubApi({ [BASE]: LIST });
    renderAt("/artifacts", <ArtifactsPage />);
    expect(await screen.findByRole("link", { name: "approver" })).toHaveAttribute("href", "/artifacts/approver");
    const row = screen.getByRole("link", { name: "approver" }).closest("tr")!;
    expect(within(row).getByText(V2)).toBeInTheDocument();
    expect(within(row).getByText("on")).toBeInTheDocument();
    expect(within(row).getByText("1")).toBeInTheDocument();
  });

  it("the row menu deletes an artifact after the code is typed", async () => {
    const api = await stubApi({ [BASE]: LIST, [`DELETE ${BASE}/approver`]: null });
    renderAt("/artifacts", <ArtifactsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("button", { name: "Delete artifact" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type the code to confirm"), { target: { value: "approver" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(api.calls("DELETE", `${BASE}/approver`)).toHaveLength(1));
  });

  it("the row menu offers no Delete without the leaf", async () => {
    await stubApi({ [BASE]: LIST }, ["versioned_artifacts"]);
    renderAt("/artifacts", <ArtifactsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    expect(await screen.findByRole("menuitem", { name: "Open" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
  });

  it.each([["en", "Artifacts", "Open drafts", "on", "Artifacts · Agento"], ["pl", "Artefakty", "Otwarte wersje robocze", "włączone", "Artefakty · Agento"]])(
    "shows the list in %s", async (tag, title, drafts, share, tab) => {
      await stubApi({ [BASE]: LIST });
      renderInLocale(tag, "/artifacts", <ArtifactsPage />);
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
      const row = (await screen.findByRole("link", { name: "approver" })).closest("tr")!;
      expect(screen.getByRole("columnheader", { name: drafts })).toBeInTheDocument();
      expect(within(row).getByText(share)).toBeInTheDocument();
      expect(document.title).toBe(tab);
    });

  it.each([
    ["a code of the module", { error: "server words", code: "TOOLBOX_UNAVAILABLE" }, "Usługa jest niedostępna. Spróbuj ponownie."],
    ["no code", { error: "server words" }, "Nie udało się usunąć artefaktu."],
  ])("a refused delete shows the Polish text for %s, never the server's", async (_case, body, text) => {
    await stubApi({ [BASE]: LIST, [`DELETE ${BASE}/approver`]: () => new Response(JSON.stringify(body), { status: 503 }) });
    renderInLocale("pl", "/artifacts", <ArtifactsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Akcje" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Usuń" }));
    fireEvent.change(await screen.findByLabelText("Wpisz kod, aby potwierdzić"), { target: { value: "approver" } });
    fireEvent.click(screen.getByRole("button", { name: "Usuń artefakt" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText("server words")).toBeNull();
  });
});

const DETAIL = (
  <Routes>
    <Route path="/artifacts/:code" element={<ArtifactPage />} /><Route path="/artifacts" element={<p>list page</p>} />
  </Routes>
);
const detailAt = () => renderAt("/artifacts/approver", DETAIL);


describe("the artifact page", () => {
  it("lists the versions with their history; only an older version can be made current", async () => {
    await stubApi({ [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS });
    detailAt();
    expect(await screen.findByText("Adds CSV export")).toBeInTheDocument();
    expect(screen.getByText("miniapp.approve")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Make current" })).toHaveLength(1);
  });

  it("Make current is a compare-and-set on the current version; a 409 is shown", async () => {
    const api = await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      [`POST ${BASE}/approver/publish`]: new Response(JSON.stringify({ error: "current changed", code: "CURRENT_VERSION_CHANGED" }), { status: 409 }),
    });
    detailAt();
    fireEvent.click(await screen.findByRole("button", { name: "Make current" }));
    expect(await screen.findByText("The current version changed. Reload the page.")).toBeInTheDocument();
    expect(screen.queryByText("current changed")).toBeNull();
    expect(api.calls("POST", `${BASE}/approver/publish`)[0].body).toEqual({ version_id: V1, expected_current_version: V2 });
  });

  it("a stale preview after Make current is shown with a repair that re-publishes the new current", async () => {
    const api = await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      [`POST ${BASE}/approver/publish`]: (body: { version_id: string }) =>
        (body.version_id === V1 && api.calls("POST", `${BASE}/approver/publish`).length > 1
          ? { current_version: V1 } : { current_version: V1, preview_stale: true }),
    });
    detailAt();
    fireEvent.click(await screen.findByRole("button", { name: "Make current" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("still shows the previous version");
    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Repair" })).toBeNull());
    expect(api.calls("POST", `${BASE}/approver/publish`)[1].body).toEqual({ version_id: V1, expected_current_version: V1 });
  });

  it("lists the approvals per view, each linking to the approval page", async () => {
    await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      "/api/miniapps/apps/approver/approvals": [{ agent_view_id: 3, approved_version_id: V1, allowed_actions: ["jira_add_comment"],
        activated_by: "service:web:user:1", activated_at: "2026-10-09T10:00:00Z" }],
    });
    detailAt();
    expect(await screen.findByRole("link", { name: "view 3" })).toHaveAttribute("href", "/apps/approver?view=3");
    expect(screen.getByText("jira_add_comment")).toBeInTheDocument();
  });

  it("hides the approvals when the miniapps route answers an error", async () => {
    const api = await stubApi({ [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      "/api/miniapps/apps/approver/approvals": new Response(JSON.stringify({ error: "not found" }), { status: 404 }) });
    detailAt();
    await screen.findByText("Adds CSV export");
    await waitFor(() => expect(api.calls("GET", "/api/miniapps/apps/approver/approvals")).toHaveLength(1));
    expect(screen.queryByText("Approvals")).toBeNull();
  });

  it("Delete needs the code typed, then goes back to the list", async () => {
    const api = await stubApi({ [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS, [`DELETE ${BASE}/approver`]: null });
    detailAt();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("button", { name: "Delete artifact" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type the code to confirm"), { target: { value: "approver" } });
    fireEvent.click(confirm);
    expect(await screen.findByText("list page")).toBeInTheDocument();
    expect(api.calls("DELETE", `${BASE}/approver`)).toHaveLength(1);
  });

  it("hides the write actions without their leaves", async () => {
    await stubApi({ [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS }, ["versioned_artifacts"]);
    detailAt();
    await screen.findByText("Adds CSV export");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Make current" })).toBeNull());
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it.each([
    ["en", "Versions", "current", "view 3 job 812 agent", "Approvals", "view 3"],
    ["pl", "Wersje", "bieżąca", "widok 3 zadanie 812 agent", "Zatwierdzenia", "widok 3"],
  ])("shows the artifact in %s", async (tag, versions, badge, history, approvals, view) => {
    await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      "/api/miniapps/apps/approver/approvals": [{ agent_view_id: 3, approved_version_id: V1, allowed_actions: ["jira_add_comment"],
        activated_by: "service:web:user:1", activated_at: "2026-10-09T10:00:00Z" }],
    });
    renderInLocale(tag, "/artifacts/approver", DETAIL);
    expect(screen.getByRole("heading", { name: "approver" })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: versions })).toBeInTheDocument();
    expect(await screen.findByText(badge)).toBeInTheDocument();
    expect(screen.getByText("Adds CSV export").closest("li")).toHaveTextContent(history);
    expect(await screen.findByRole("heading", { name: approvals })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: view })).toBeInTheDocument();
    expect(document.title).toBe("approver · Agento");
  });

  it.each([
    ["a code of the module", { error: "server words", code: "CURRENT_VERSION_CHANGED" }, "Bieżąca wersja się zmieniła. Odśwież stronę."],
    ["no code", { error: "server words" }, "Nie udało się ustawić wersji jako bieżącej."],
  ])("a refused Make current shows the Polish text for %s, never the server's", async (_case, body, text) => {
    await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      [`POST ${BASE}/approver/publish`]: () => new Response(JSON.stringify(body), { status: 409 }),
    });
    renderInLocale("pl", "/artifacts/approver", DETAIL);
    fireEvent.click(await screen.findByRole("button", { name: "Ustaw jako bieżącą" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText("server words")).toBeNull();
  });

  it("a stale preview names the version inside the Polish sentence", async () => {
    await stubApi({
      [BASE]: LIST, [`${BASE}/approver/versions`]: VERSIONS,
      [`POST ${BASE}/approver/publish`]: { current_version: V1, preview_stale: true },
    });
    renderInLocale("pl", "/artifacts/approver", DETAIL);
    fireEvent.click(await screen.findByRole("button", { name: "Ustaw jako bieżącą" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(`Wersja ${V1} jest bieżąca, ale witryna nadal pokazuje poprzednią.`);
    expect(screen.getByRole("button", { name: "Napraw" })).toBeInTheDocument();
  });
});
