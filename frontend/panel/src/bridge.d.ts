// Types for the E6 panel-side bridge (src/agento/modules/miniapps/sdk/bridge.js).
export interface ActionResult { status: number; body: unknown }
export function createLaunchBridge(opts: {
  appWindow: Window;
  appsOrigin: string;
  launchId: string;
  onAction: (tool: string, args: Record<string, unknown>) => Promise<ActionResult>;
  window?: Window;
}): { close(): void };
