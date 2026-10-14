import { describe, expect, it } from "vitest";
import { dayGroup } from "./model";

describe("dayGroup", () => {
  const now = new Date(2026, 9, 6, 9, 0);
  const at = (d: number, h = 12) => new Date(2026, 9, d, h).toISOString();
  it("groups by the local calendar day", () => {
    expect(dayGroup(new Date(2026, 9, 6, 0, 1).toISOString(), now)).toBe("Today");
    expect(dayGroup(at(5, 23), now)).toBe("Yesterday");
    expect(dayGroup(at(4), now)).toBe("Previous 7 days");
    expect(dayGroup(new Date(2026, 8, 28).toISOString(), now)).toBe("Older");
    expect(dayGroup(null, now)).toBe("Older");
  });
});
