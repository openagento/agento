import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { I18nProvider, type Messages } from "@agento/ui";
import { ApiError } from "./errors";
import { errorCode, useErrorText } from "./errorText";

const POLISH: Messages = {
  panel: { "error.UPLOAD_TOO_LARGE": "Plik jest zbyt duży (najwyżej {max_bytes} B).", "error.FORBIDDEN": "Brak uprawnień.", "error.SHARED": "Wspólny błąd.", "save.failed": "Nie udało się zapisać." },
  scheduler: { "error.upload": "Nie udało się przesłać pliku.", "error.OWN_REASON": "Powód harmonogramu.", "error.SHARED": "Błąd harmonogramu.", "error.TOO_MANY": "Najwyżej {max} harmonogramów.", "save.failed": "Nie zapisano harmonogramu." },
};
const refusal = (body: unknown) => new ApiError(400, "server text that must not be shown", undefined, body);
const errorText = (namespace?: string) => renderHook(() => useErrorText(namespace), {
  wrapper: ({ children }: { children: ReactNode }) => <I18nProvider locale="pl" messages={POLISH}>{children}</I18nProvider>,
}).result.current;

describe("useErrorText", () => {
  it.each([
    ["a code of the screen's namespace", "scheduler", { code: "SHARED" }, "Błąd harmonogramu."],
    ["a code only `panel` knows", "scheduler", { code: "FORBIDDEN" }, "Brak uprawnień."],
    ["a code with typed params", "scheduler", { code: "TOO_MANY", params: { max: 5, extra: { nested: true } } }, "Najwyżej 5 harmonogramów."],
    ["an unknown code", "scheduler", { code: "NEVER_HEARD_OF" }, "Nie zapisano harmonogramu."],
    ["a reason only `panel` knows, with a number in the language's digits", "scheduler",
      { code: "upload", reason: "UPLOAD_TOO_LARGE", params: { max_bytes: 26214400 } }, "Plik jest zbyt duży (najwyżej 26\u00a0214\u00a0400 B)."],
    ["a reason of the screen's namespace", "scheduler", { code: "upload", reason: "OWN_REASON" }, "Powód harmonogramu."],
    ["an unknown reason reads the code", "scheduler", { code: "upload", reason: "NEVER_HEARD_OF" }, "Nie udało się przesłać pliku."],
    ["a reason that is not a text reads the code", "scheduler", { code: "upload", reason: 7 }, "Nie udało się przesłać pliku."],
    ["a known reason with an unknown code", "scheduler", { code: "NEVER_HEARD_OF", reason: "FORBIDDEN" }, "Brak uprawnień."],
    ["a body with no code", "scheduler", { error: "boom" }, "Nie zapisano harmonogramu."],
    ["no body", "scheduler", undefined, "Nie zapisano harmonogramu."],
    ["the panel namespace by default", undefined, { code: "SHARED" }, "Wspólny błąd."],
  ])("%s", (_name, namespace, body, text) => {
    expect(errorText(namespace)(refusal(body), "save.failed")).toBe(text);
  });

  it("an error that is not an API refusal reads the fallback key", () => {
    expect(errorText()(new TypeError("offline"), "save.failed")).toBe("Nie udało się zapisać.");
  });

  it("errorCode reads only a text code of an API refusal", () => {
    expect([errorCode(refusal({ code: "X" })), errorCode(refusal({ code: 7 })), errorCode(new Error("x"))]).toEqual(["X", undefined, undefined]);
  });
});
