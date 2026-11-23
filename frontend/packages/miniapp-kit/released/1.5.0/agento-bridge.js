// The app side of the miniapp bridge (PRD E6 §8). An app loads this file and calls
// `createAgentoSdk({ panelOrigin })`. `panelOrigin` is a trusted input the app itself
// gives (from its agent_view instructions) — never a value read from a message or the
// URL. Every message is accepted only from `window.opener` at `panelOrigin`, and every
// post names `panelOrigin` as its target, never '*'.
//
// Protocol: app → `{type:'agento.ready'}`; panel → `{type:'agento.hello', launch_id}`;
// app → `{type:'agento.action', launch_id, id, tool, arguments}`;
// panel → `{type:'agento.result', launch_id, id, status, body}`.

// A panel that stops answering must not grow `pending` for the app's lifetime (CODE-8).
export const MAX_IN_FLIGHT = 16;

/** `v` when it is one exact origin (`https:`, or `http:` on localhost); else null. */
export function exactOrigin(v) {
  if (typeof v !== 'string') return null;
  let url;
  try { url = new URL(v); } catch { return null; }
  if (url.origin !== v) return null;
  if (url.protocol === 'https:' || (url.protocol === 'http:' && url.hostname === 'localhost')) return v;
  return null;
}

export function createAgentoSdk({ panelOrigin, window: w = globalThis.window } = {}) {
  const origin = exactOrigin(panelOrigin);
  if (!origin) throw new Error('agento-sdk: panelOrigin must be one exact https origin');
  const panel = w?.opener;
  if (!panel) throw new Error('agento-sdk: this page must be opened by the Agento panel');

  let launchId = null;
  let resolveReady;
  const ready = new Promise((r) => { resolveReady = r; });
  const pending = new Map();
  let next = 0;

  function onMessage(event) {
    if (event.source !== panel || event.origin !== origin) return;
    const m = event.data;
    if (!m || typeof m !== 'object') return;
    if (m.type === 'agento.hello' && launchId === null && typeof m.launch_id === 'string') {
      launchId = m.launch_id;
      resolveReady(launchId);
    } else if (m.type === 'agento.result' && launchId !== null && m.launch_id === launchId && pending.has(m.id)) {
      const settle = pending.get(m.id);
      pending.delete(m.id);
      settle({ status: m.status, body: m.body });
    }
  }
  w.addEventListener('message', onMessage);
  panel.postMessage({ type: 'agento.ready' }, origin);

  return {
    /** The launch id the panel bound this window to. */
    ready,
    /** Call one of the launch's allowed actions; resolves `{status, body}`. At most
     *  `MAX_IN_FLIGHT` calls wait — for the handshake or for an answer; one more resolves
     *  `{status: 429}`. The slot is taken before the handshake, so no wait is unbounded. */
    callAction(tool, args = {}) {
      if (pending.size >= MAX_IN_FLIGHT) return Promise.resolve({ status: 429, body: null });
      const id = (next += 1);
      const result = new Promise((settle) => { pending.set(id, settle); });
      ready.then(() => {
        if (pending.has(id)) panel.postMessage({ type: 'agento.action', launch_id: launchId, id, tool, arguments: args }, origin);
      });
      return result;
    },
    close() { w.removeEventListener('message', onMessage); pending.clear(); },
  };
}
