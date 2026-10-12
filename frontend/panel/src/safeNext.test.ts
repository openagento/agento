import { describe, expect, it } from "vitest";
import { safeNext } from "./safeNext";

describe("safeNext", () => {
  it.each([
    ["/conversations/3?x=1", "/conversations/3?x=1"], [null, "/"], ["//evil.example", "/"],
    ["https://evil.example/", "/"], ["/\\evil.example", "/"], ["/login?next=/x", "/"], ["javascript:alert(1)", "/"],
  ])("%s → %s", (input, out) => expect(safeNext(input)).toBe(out));
});
