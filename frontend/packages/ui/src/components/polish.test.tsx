import { fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import pl from "../../../../../src/agento/framework/i18n/ui/pl.json";
import { I18nProvider, useFormat } from "../i18n";
import { AgentoUiProvider } from "../Provider";
import { ChatComposer, ChatError, ChatLayout, ChatMessage, Reasoning, SubagentRun, ToolCall, ToolGroup } from "./Chat";
import { SearchToggle, ThreadList } from "./ChatShell";
import { ConfirmDialog } from "./ConfirmDialog";
import { ConnectionStatus } from "./ConnectionStatus";
import { CopyButton } from "./CopyButton";
import { DataTable } from "./DataTable";
import { Pager } from "./Grid";
import { EditableTitle } from "./Headers";
import { ArchiveAction, DeleteAction, DetailsAction, EndAction, MakeCurrentAction, OpenAction, PreviewAction, RegenerateAction, SettingsAction } from "./IconAction";
import { JobDetail, type JobDetailData } from "./JobDetail";
import { JobStatusCard } from "./JobStatusCard";
import { Markdown } from "./Markdown";
import { SideDrawer } from "./Panel";
import { RowMenu } from "./RowMenu";
import { ErrorState, LoadingState } from "./States";
import { AppTile, IconPicker, ViewToggle } from "./Tiles";

const NBSP = String.fromCharCode(0xa0);
const JOB: JobDetailData = {
  id: 7, type: "cron", status: "SUCCESS", source: "jira", reference_id: null, agent_view_code: "support", agent_type: "claude",
  model: "m-1", input_tokens: 12345, output_tokens: 678, created_at: null, started_at: "2026-01-01T10:00:00Z",
  finished_at: "2026-01-01T10:03:05Z", error_class: "Boom", error_message: "bad", result_summary: "done", prompt: "p", output: "o",
};
const inPolish = (ui: ReactNode) =>
  render(<I18nProvider locale="pl" messages={{ ui: pl }}><AgentoUiProvider env="test">{ui}</AgentoUiProvider></I18nProvider>);

describe("the components in Polish", () => {
  it("the states", () => {
    inPolish(<><LoadingState /><ErrorState onRetry={() => {}} /></>);
    expect(screen.getByRole("status")).toHaveTextContent("Ładowanie…");
    expect(screen.getByRole("alert")).toHaveTextContent("Coś poszło nie tak");
    expect(screen.getByRole("button", { name: "Spróbuj ponownie" })).toBeInTheDocument();
  });

  it("an empty table", () => {
    inPolish(<DataTable caption="x" columns={[]} rows={[]} rowKey={() => "k"} />);
    expect(screen.getByText("Nic tu jeszcze nie ma")).toBeInTheDocument();
  });

  it("the pager, with Polish numbers", () => {
    inPolish(<Pager page={2} pageSize={50} total={12345} onPage={() => {}} />);
    expect(screen.getByText(`51–100 z 12${NBSP}345`, { normalizer: (exact) => exact })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Poprzednia strona" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Następna strona" })).toBeInTheDocument();
  });

  it("a dialog's default buttons and its close button", () => {
    inPolish(<ConfirmDialog opened title="Tytuł" onConfirm={() => {}} onCancel={() => {}} />);
    expect(screen.getAllByRole("button").map((b) => b.textContent || b.getAttribute("aria-label")))
      .toEqual(["Zamknij", "Anuluj", "Potwierdź"]);
  });

  it("a drawer's close button", () => {
    inPolish(<SideDrawer opened onClose={() => {}} title="Tytuł">x</SideDrawer>);
    expect(screen.getByRole("button", { name: "Zamknij" })).toBeInTheDocument();
  });

  it("the default name of each icon action", () => {
    inPolish(<><CopyButton value="x" /><ArchiveAction /><RegenerateAction /><OpenAction /><PreviewAction /><DetailsAction />
      <MakeCurrentAction /><EndAction /><DeleteAction /><SettingsAction /></>);
    expect(screen.getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "Kopiuj", "Archiwizuj", "Wygeneruj ponownie", "Otwórz", "Podgląd", "Szczegóły", "Ustaw jako bieżącą", "Zakończ", "Usuń",
      "Ustawienia",
    ]);
  });

  it("an editable title names its pencil and its field", () => {
    inPolish(<EditableTitle value="x" onSave={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Edytuj: tytuł" }));
    expect(screen.getByRole("textbox", { name: "Tytuł" })).toHaveValue("x");
  });

  it("a given name wins over the default", () => {
    inPolish(<DeleteAction label="Usuń rolę" />);
    expect(screen.getByRole("button", { name: "Usuń rolę" })).toBeInTheDocument();
  });

  it("the chat: the pane, a message, the tools, the reasoning and a failed turn", () => {
    inPolish(
      <ChatLayout title="t" onFiles={() => {}}>
        <ChatMessage author="assistant" text="hello" />
        <ToolCall summary="ls" running={false} isError input="a" output="b" />
        <ToolGroup count={1} running={false}>x</ToolGroup>
        <ToolGroup count={3} running={false}>x</ToolGroup>
        <ToolGroup count={5} running={false}>x</ToolGroup>
        <SubagentRun label="child" state="published" />
        <Reasoning text="" live />
        <Reasoning text="" live={false} />
        <Reasoning text="" live={false} seconds={4} />
        <ChatError title="x" details="raw" onRetry={() => {}} />
      </ChatLayout>);
    expect(screen.getByRole("region", { name: "Rozmowa" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Oś czasu" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Kopiuj wiadomość" })).toBeInTheDocument();
    expect(screen.getByLabelText("nieudane")).toBeInTheDocument();
    for (const text of ["Użyto 1 narzędzia", "Użyto 3 narzędzi", "Użyto 5 narzędzi", "W kolejce", "Myśli…", "Przemyślenia",
      "Czas namysłu: 4 s", "Szczegóły"]) expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Spróbuj ponownie" })).toBeInTheDocument();
  });

  it("the composer and its files", () => {
    const { unmount } = inPolish(<ChatComposer value="" onChange={() => {}} onSend={() => {}} canSend onFiles={() => {}} onRemoveAttachment={() => {}}
      attachments={[{ id: "1", name: "a.pdf", status: "ready", bytes: 1536 }, { id: "2", name: "b.txt", status: "error" }]} />);
    expect(screen.getByRole("textbox", { name: "Wiadomość" })).toHaveAttribute("placeholder", "Zapytaj agenta…");
    expect(screen.getByRole("button", { name: "Dodaj pliki" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Wyślij" })).toBeInTheDocument();
    expect(screen.getByLabelText("Załączone pliki")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Usuń a.pdf" })).toBeInTheDocument();
    expect(screen.getByText("1,5 KB")).toBeInTheDocument();
    expect(screen.getByText("nie udało się przesłać")).toBeInTheDocument();
    unmount();
    inPolish(<ChatComposer value="" onChange={() => {}} onSend={() => {}} canSend={false} stoppable onStop={() => {}} />);
    expect(screen.getByRole("button", { name: "Zatrzymaj" })).toBeInTheDocument();
  });

  it("the thread list and its search button", async () => {
    const { unmount } = inPolish(<><SearchToggle open={false} onToggle={() => {}} />
      <ThreadList groups={[{ id: "g", label: "g", threads: [{ id: 1, title: "First", live: true, pinned: true, onSelect: () => {}, onPin: () => {}, onRename: () => {} }] }]} /></>);
    expect(screen.getByRole("button", { name: "Szukaj rozmów" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "w toku" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Odepnij" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Akcje rozmowy" }));
    expect(await screen.findByRole("menuitem", { name: "Zmień nazwę" })).toBeInTheDocument();
    unmount();
    inPolish(<ThreadList groups={[]} />);
    expect(screen.getByText("Nie ma jeszcze rozmów.")).toBeInTheDocument();
  });

  it("a job's facts, with Polish numbers and duration", () => {
    inPolish(<JobDetail job={JOB} />);
    expect(screen.getAllByRole("rowheader").map((th) => th.textContent)).toEqual(["Status", "Typ", "Źródło", "Widok agenta", "Odniesienie",
      "Agent", "Model", "Tokeny wej. / wyj.", "Utworzono", "Rozpoczęto", "Zakończono", "Czas trwania", "Klasa błędu"]);
    expect(screen.getByText(`12${NBSP}345 / 678`, { normalizer: (exact) => exact })).toBeInTheDocument();
    expect(screen.getByText("3 min 05 s")).toBeInTheDocument();
    for (const text of ["Błąd", "Rezultat", "Prompt", "Wynik", "Ponowne uruchomienie"]) expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each([["TODO", "Oczekuje"], ["RUNNING", "W toku"], ["SUCCESS", "Zakończone"], ["FAILED", "Nieudane"], ["DEAD", "Porzucone"],
    ["PAUSED", "Wstrzymane"], ["NEW_ONE", "NEW_ONE"]])("the job status %s reads %s", (status, text) => {
    inPolish(<JobDetail job={{ ...JOB, status }} />);
    expect(screen.getAllByRole("cell")[0]).toHaveTextContent(text);
  });

  it.each([[42, "42 s", "42s"], [185, "3 min 05 s", "3m 05s"], [3720, "1 godz. 02 min", "1h 02m"], [null, "—", "—"]])(
    "the duration %s reads %s, and in English %s", (seconds, text, english) => {
      const polish = renderHook(() => useFormat(), {
        wrapper: ({ children }: { children: ReactNode }) => <I18nProvider locale="pl" messages={{ ui: pl }}>{children}</I18nProvider> });
      expect(polish.result.current.duration(seconds)).toBe(text);
      expect(renderHook(() => useFormat()).result.current.duration(seconds)).toBe(english);
    });

  it("a job card and the connection state", () => {
    inPolish(<><JobStatusCard title="x" state="blocked" updatedAt="2026-01-01T10:00:00Z" />
      <ConnectionStatus state="refused" onReconnect={() => {}} /></>);
    expect(screen.getByText("Zablokowane")).toBeInTheDocument();
    expect(screen.getByText(/^Zaktualizowano/)).toBeInTheDocument();
    expect(screen.getByText("Rozłączono")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Połącz ponownie" })).toBeInTheDocument();
  });

  it("the tiles, the row menu and a Markdown table", async () => {
    inPolish(<><ViewToggle value="tiles" onChange={() => {}} /><IconPicker value={null} onChange={() => {}} />
      <AppTile icon={null} title="Zablokowana" disabled onOpen={() => {}} /><RowMenu actions={[{ id: "x", label: "x", onClick: () => {} }]} />
      <Markdown highlight>{"| a |\n|---|\n| 1 |"}</Markdown></>);
    expect(screen.getByRole("radiogroup", { name: "Widok" })).toBeInTheDocument();
    for (const text of ["Kafelki", "Lista"]) expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zmień ikonę" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zablokowana" })).toHaveAttribute("title", "Możesz zobaczyć tę aplikację, ale nie możesz jej uruchomić");
    expect(screen.getByRole("button", { name: "Akcje" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Tabela" }, { timeout: 10_000 })).toBeInTheDocument();
  }, 15_000);
});
