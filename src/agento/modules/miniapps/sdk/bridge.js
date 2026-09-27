// The panel side of the miniapp bridge (PRD E6 §8). All apps share one origin, so
// `event.origin` cannot tell two apps apart: a message counts only when it comes from
// the exact window the panel opened (`event.source === appWindow`), from the apps
// origin, and — past the handshake — carries this launch's id. Replies target the
// apps origin, never '*'. `onAction(tool, args)` calls the panel's action endpoint
// (`POST /api/launches/<id>/actions/<tool>`) and resolves `{status, body}`.
import { exactOrigin } from './agento-sdk.js';

export function createLaunchBridge({ appWindow, appsOrigin, launchId, onAction, window: w = globalThis.window } = {}) {
  const origin = exactOrigin(appsOrigin);
  if (!origin) throw new Error('bridge: appsOrigin must be one exact https origin');
  if (!appWindow) throw new Error('bridge: appWindow is required');
  if (typeof launchId !== 'string' || !launchId) throw new Error('bridge: launchId is required');
  const reply = (m) => appWindow.postMessage({ ...m, launch_id: launchId }, origin);

  async function onMessage(event) {
    if (event.source !== appWindow || event.origin !== origin) return;
    const m = event.data;
    if (!m || typeof m !== 'object') return;
    // `ready` carries no data and gets only the launch id this window was opened for.
    if (m.type === 'agento.ready') { reply({ type: 'agento.hello' }); return; }
    if (m.type !== 'agento.action' || m.launch_id !== launchId || typeof m.tool !== 'string') return;
    let result;
    try {
      result = await onAction(m.tool, m.arguments ?? {});
    } catch {
      result = { status: 503, body: { ok: false, error: { code: 'unavailable', message: 'the panel could not call the action' } } };
    }
    reply({ type: 'agento.result', id: m.id, status: result.status, body: result.body });
  }
  w.addEventListener('message', onMessage);
  return { close() { w.removeEventListener('message', onMessage); } };
}
