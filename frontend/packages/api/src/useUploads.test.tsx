import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@agento/ui";
import pl from "../../../../src/agento/framework/i18n/panel/pl.json";
import { endSession, login } from "./session";
import { useUploads } from "./useUploads";

const MESSAGES = { panel: pl, scheduler: { "error.UPLOAD_TOO_LARGE": "Plik harmonogramu jest zbyt duży." } };
const SESSION = { user: { id: 1, username: "ann", role: "user", is_active: true, acl: [], ui_locale: null }, csrf_token: "t", expires_at: "x" };
const refuse = async (body: object) => {
  vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => (init?.body instanceof FormData
    ? new Response(JSON.stringify({ error: "server words", ...body }), { status: 400 }) : new Response(JSON.stringify(SESSION)))));
  await login("ann", "pw");
};
const failedUpload = async (namespace?: string) => {
  const { result } = renderHook(() => useUploads("/api/x/uploads", [], namespace), {
    wrapper: ({ children }: { children: ReactNode }) => <I18nProvider locale="pl" messages={MESSAGES}>{children}</I18nProvider>,
  });
  act(() => result.current.add([new File(["x"], "a.txt")]));
  await waitFor(() => expect(result.current.files[0].status).toBe("error"));
  return result.current.files[0].error;
};
afterEach(() => { endSession(); vi.unstubAllGlobals(); });

describe("useUploads in Polish", () => {
  it.each([
    ["a known code", { code: "UPLOAD_TOO_LARGE", params: { max_bytes: 512 } }, undefined, "Plik jest zbyt duży (najwyżej 512 B)."],
    ["a code the caller's namespace holds", { code: "UPLOAD_TOO_LARGE" }, "scheduler", "Plik harmonogramu jest zbyt duży."],
    ["a code of `panel` under another namespace", { code: "UPLOAD_EMPTY" }, "scheduler", "Plik jest pusty."],
    ["no code", {}, undefined, "Nie udało się przesłać pliku. Spróbuj ponownie."],
    ["no code under a namespace with no text of its own", {}, "scheduler", "Nie udało się przesłać pliku. Spróbuj ponownie."],
  ])("a failed file reads the text of %s, never the server's words", async (_name, body, namespace, text) => {
    await refuse(body);
    expect(await failedUpload(namespace)).toBe(text);
  });
});
