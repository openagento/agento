import { describe, expect, it } from "vitest";
import { dayGroup, jobTree, type SubJob } from "./model";

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

describe("jobTree", () => {
  const job = (id: number, parent: number | null, depth: number): SubJob => ({ id, parent_job_id: parent, depth, status: "SUCCESS" });
  it("nests each job under its parent, oldest first; a child of a job not loaded is a root", () => {
    const tree = jobTree([job(14, 11, 2), job(13, 99, 2), job(12, 11, 2), job(11, 10, 1)]);
    expect(tree.map((n) => n.job.id)).toEqual([11, 13]);
    expect(tree[0].children.map((n) => n.job.id)).toEqual([12, 14]);
  });
});
