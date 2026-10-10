// The launch handshake (PRD E2 §4.3, E6 §8). A miniapp runs in its own window (the E6
// SDK needs `window.opener`). The one-time exchange code is used once, inside
// `openLaunch()`, to build a form POST: it never enters a URL, React state, the query or
// mutation cache, a log or the window name.
import { apiFetch, ApiError, onEndSession, queryClient } from "@agento/api";
import { createLaunchBridge, type ActionResult } from "@agento/miniapp-bridge";

export interface LaunchRequest { agentViewId: number; artifactCode: string }

export type LaunchOutcome =
  | { kind: "reused"; launch_id: string }
  | { kind: "opened"; launch_id: string; artifact_code: string; version_id: string }
  | { kind: "blocked" };

interface Created {
  launch_id: string; artifact_code: string; version_id: string;
  redeem: { url: string; fields: Record<string, string> };
}

interface Tracked {
  key: string; window: Window; bridge: { close(): void }; timer: ReturnType<typeof setInterval>; createdAt: number;
}

const tracked = new Map<string, Tracked>();
const keyOf = (r: LaunchRequest) => `${r.agentViewId}:${r.artifactCode}`;

function drop(id: string, closeWindow = false): void {
  const t = tracked.get(id);
  if (!t) return;
  clearInterval(t.timer);
  t.bridge.close();
  if (closeWindow) t.window.close();
  tracked.delete(id);
}

onEndSession(() => { for (const id of [...tracked.keys()]) drop(id, true); });

export const trackedLaunchIds = () => [...tracked.keys()];

/** Bound the tracked set by the server's (CODE-8): a launch the server no longer lists —
 *  revoked, evicted or expired — has its window and bridge closed. `requestedAt` is when that
 *  list was requested; a launch made after it may be missing from it and is kept. */
export function reconcileLaunches(liveIds: ReadonlySet<string>, requestedAt: number): void {
  for (const [id, t] of [...tracked]) {
    if (!liveIds.has(id) && t.createdAt < requestedAt) drop(id, true);
  }
}

async function action(launchId: string, tool: string, args: Record<string, unknown>): Promise<ActionResult> {
  try {
    const body = await apiFetch(`/api/launches/${launchId}/actions/${encodeURIComponent(tool)}`,
      { method: "POST", json: { arguments: args } });
    return { status: 200, body };
  } catch (e) {
    if (e instanceof ApiError) return { status: e.status, body: e.body ?? null };
    throw e;
  }
}

function submitForm(url: string, target: string, fields: Record<string, string>, doc: Document): void {
  const form = doc.createElement("form");
  form.method = "post";
  form.action = url;
  form.target = target;
  form.hidden = true;
  for (const [name, value] of Object.entries(fields)) {
    const input = doc.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  doc.body.append(form);
  form.submit();
  form.remove();
}

async function create(req: LaunchRequest, appWindow: Window, name: string, w: Window): Promise<LaunchOutcome> {
  let created: Created;
  try {
    created = await apiFetch<Created>("/api/launches", {
      method: "POST", json: { agent_view_id: req.agentViewId, artifact_code: req.artifactCode },
    });
  } catch (e) {
    appWindow.close();
    throw e;
  }
  const { redeem, launch_id, artifact_code, version_id } = created;
  let bridge: { close(): void };
  try {
    // Installed before the redeem, so the app's first `agento.ready` is answered.
    bridge = createLaunchBridge({
      appWindow, appsOrigin: new URL(redeem.url).origin, launchId: launch_id,
      onAction: (tool, args) => action(launch_id, tool, args), window: w,
    });
  } catch (e) {
    appWindow.close();
    throw e;
  }
  const timer = setInterval(() => { if (appWindow.closed) drop(launch_id); }, 1_000);
  tracked.set(launch_id, { key: keyOf(req), window: appWindow, bridge, timer, createdAt: Date.now() });
  submitForm(redeem.url, name, redeem.fields, w.document);
  void queryClient.invalidateQueries({ queryKey: ["launches"] });
  return { kind: "opened", launch_id, artifact_code, version_id };
}

/** Call from the click handler itself: the window is reserved synchronously, before any
 *  await, or the browser blocks it. `liveIds` are the ids `GET /api/launches` answered. */
export function openLaunch(req: LaunchRequest, liveIds: ReadonlySet<string>, w: Window = window): Promise<LaunchOutcome> {
  for (const [id, t] of tracked) {
    if (t.key === keyOf(req) && !t.window.closed && liveIds.has(id)) {
      // A redeemed code is single-use: a live launch is focused, never redeemed again.
      t.window.focus();
      return Promise.resolve({ kind: "reused", launch_id: id });
    }
  }
  const name = `agento-app-${crypto.randomUUID()}`;
  const appWindow = w.open("about:blank", name);
  if (!appWindow) return Promise.resolve({ kind: "blocked" });
  return create(req, appWindow, name, w);
}

/** End a launch: the API revokes it, and its window and bridge are closed here. */
export async function endLaunch(id: string): Promise<void> {
  await apiFetch(`/api/launches/${id}`, { method: "DELETE" });
  drop(id, true);
  void queryClient.invalidateQueries({ queryKey: ["launches"] });
}
