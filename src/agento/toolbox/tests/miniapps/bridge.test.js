import { describe, it, expect, vi } from 'vitest';
import { createLaunchBridge } from '../../../modules/miniapps/sdk/bridge.js';
import { createAgentoSdk, exactOrigin, MAX_IN_FLIGHT } from '../../../modules/miniapps/sdk/agento-sdk.js';

const PANEL = 'https://panel.example.com';
const APPS = 'https://apps.example.com';
const LID = 'a'.repeat(32);

function fakeWindow(opener = null) {
  const listeners = new Set();
  return {
    opener,
    postMessage: vi.fn(),
    addEventListener: (type, fn) => type === 'message' && listeners.add(fn),
    removeEventListener: (type, fn) => listeners.delete(fn),
    async deliver(event) { for (const fn of [...listeners]) await fn(event); },
  };
}

describe('exactOrigin', () => {
  it.each([PANEL, 'http://localhost:8443', 'https://panel.localhost:8443'])('accepts %s', (v) => {
    expect(exactOrigin(v)).toBe(v);
  });
  it.each(['*', 'null', `${PANEL}/`, `${PANEL}/path`, 'http://panel.example.com', 'javascript:alert(1)', '', null, 42])(
    'refuses %s', (v) => { expect(exactOrigin(v)).toBeNull(); });
});

describe('panel bridge', () => {
  const setup = (onAction = vi.fn(async () => ({ status: 200, body: { ok: true } }))) => {
    const panel = fakeWindow();
    const app = fakeWindow();
    createLaunchBridge({ appWindow: app, appsOrigin: APPS, launchId: LID, onAction, window: panel });
    return { panel, app, onAction };
  };
  const action = (over = {}) => ({ type: 'agento.action', launch_id: LID, id: 1, tool: 'notes_add', arguments: { x: 1 }, ...over });

  it('answers ready with the launch id, targeting the apps origin', async () => {
    const { panel, app } = setup();
    await panel.deliver({ source: app, origin: APPS, data: { type: 'agento.ready' } });
    expect(app.postMessage).toHaveBeenCalledWith({ type: 'agento.hello', launch_id: LID }, APPS);
  });

  it('runs an action from its own window and replies to the apps origin', async () => {
    const { panel, app, onAction } = setup();
    await panel.deliver({ source: app, origin: APPS, data: action() });
    expect(onAction).toHaveBeenCalledWith('notes_add', { x: 1 });
    expect(app.postMessage).toHaveBeenCalledWith(
      { type: 'agento.result', id: 1, status: 200, body: { ok: true }, launch_id: LID }, APPS);
  });

  it.each([
    ['another app window on the same origin', () => ({ source: fakeWindow(), origin: APPS, data: action() })],
    ['a wrong origin', (app) => ({ source: app, origin: 'https://evil.example.com', data: action() })],
    ['a wrong launch id', (app) => ({ source: app, origin: APPS, data: action({ launch_id: 'b'.repeat(32) }) })],
    ['no launch id', (app) => ({ source: app, origin: APPS, data: action({ launch_id: undefined }) })],
    ['a non-object message', (app) => ({ source: app, origin: APPS, data: 'agento.action' })],
  ])('ignores %s', async (_name, event) => {
    const { panel, app, onAction } = setup();
    await panel.deliver(event(app));
    expect(onAction).not.toHaveBeenCalled();
    expect(app.postMessage).not.toHaveBeenCalled();
  });

  it.each([
    ['another app window on the same origin', () => ({ source: fakeWindow(), origin: APPS })],
    ['a wrong origin', (app) => ({ source: app, origin: 'https://evil.example.com' })],
  ])('gives no launch id to a ready from %s', async (_name, event) => {
    const { panel, app } = setup();
    await panel.deliver({ ...event(app), data: { type: 'agento.ready' } });
    expect(app.postMessage).not.toHaveBeenCalled();
  });

  it('never posts with a wildcard target', async () => {
    const { panel, app } = setup(vi.fn(async () => { throw new Error('down'); }));
    await panel.deliver({ source: app, origin: APPS, data: { type: 'agento.ready' } });
    await panel.deliver({ source: app, origin: APPS, data: action() });
    expect(app.postMessage.mock.calls.map(([, t]) => t)).toEqual([APPS, APPS]);
    expect(app.postMessage.mock.calls[1][0].status).toBe(503);
  });

  it('bounds the actions in flight: past the cap an action gets 429 and calls nothing', async () => {
    const { panel, app, onAction } = setup(vi.fn(() => new Promise(() => {})));
    for (let i = 1; i <= MAX_IN_FLIGHT; i++) panel.deliver({ source: app, origin: APPS, data: action({ id: i }) });
    await panel.deliver({ source: app, origin: APPS, data: action({ id: 99 }) });
    expect(onAction).toHaveBeenCalledTimes(MAX_IN_FLIGHT);
    expect(app.postMessage).toHaveBeenCalledWith({ type: 'agento.result', id: 99, status: 429, body: null, launch_id: LID }, APPS);
  });

  it('refuses a wildcard or invalid apps origin', () => {
    for (const appsOrigin of ['*', `${APPS}/`, 'http://apps.example.com']) {
      expect(() => createLaunchBridge({ appWindow: fakeWindow(), appsOrigin, launchId: LID, onAction: vi.fn(), window: fakeWindow() }))
        .toThrow(/appsOrigin/);
    }
  });
});

describe('app sdk', () => {
  const setup = () => {
    const panel = fakeWindow();
    const app = fakeWindow(panel);
    const sdk = createAgentoSdk({ panelOrigin: PANEL, window: app });
    return { panel, app, sdk };
  };
  const hello = (panel) => ({ source: panel, origin: PANEL, data: { type: 'agento.hello', launch_id: LID } });

  it('announces itself to the opener at the panel origin', () => {
    const { panel } = setup();
    expect(panel.postMessage).toHaveBeenCalledWith({ type: 'agento.ready' }, PANEL);
  });

  it('calls an action after the handshake and resolves the matching result', async () => {
    const { panel, app, sdk } = setup();
    await app.deliver(hello(panel));
    expect(await sdk.ready).toBe(LID);
    const call = sdk.callAction('notes_add', { x: 1 });
    await vi.waitFor(() => expect(panel.postMessage).toHaveBeenCalledTimes(2));
    expect(panel.postMessage).toHaveBeenLastCalledWith(
      { type: 'agento.action', launch_id: LID, id: 1, tool: 'notes_add', arguments: { x: 1 } }, PANEL);
    await app.deliver({ source: panel, origin: PANEL, data: { type: 'agento.result', launch_id: LID, id: 1, status: 200, body: { ok: true } } });
    expect(await call).toEqual({ status: 200, body: { ok: true } });
  });

  it.each([
    ['a window that is not the opener', (panel) => ({ ...hello(panel), source: fakeWindow() })],
    ['a wrong origin', (panel) => ({ ...hello(panel), origin: 'https://evil.example.com' })],
  ])('ignores a handshake from %s', async (_name, event) => {
    const { panel, app, sdk } = setup();
    await app.deliver(event(panel));
    const seen = await Promise.race([sdk.ready, new Promise((r) => setTimeout(() => r('none'), 20))]);
    expect(seen).toBe('none');
  });

  it('ignores a result for another launch', async () => {
    const { panel, app, sdk } = setup();
    await app.deliver(hello(panel));
    const call = sdk.callAction('notes_add');
    await vi.waitFor(() => expect(panel.postMessage).toHaveBeenCalledTimes(2));
    await app.deliver({ source: panel, origin: PANEL, data: { type: 'agento.result', launch_id: 'b'.repeat(32), id: 1, status: 200, body: {} } });
    const seen = await Promise.race([call, new Promise((r) => setTimeout(() => r('none'), 20))]);
    expect(seen).toBe('none');
  });

  it('bounds the calls in flight: past the cap a call resolves 429 and posts nothing', async () => {
    const { panel, app, sdk } = setup();
    await app.deliver(hello(panel));
    for (let i = 0; i < MAX_IN_FLIGHT; i++) sdk.callAction('notes_add');
    await vi.waitFor(() => expect(panel.postMessage).toHaveBeenCalledTimes(1 + MAX_IN_FLIGHT));
    expect(await sdk.callAction('notes_add')).toEqual({ status: 429, body: null });
    expect(panel.postMessage).toHaveBeenCalledTimes(1 + MAX_IN_FLIGHT);
    // A result frees a slot.
    await app.deliver({ source: panel, origin: PANEL, data: { type: 'agento.result', launch_id: LID, id: 1, status: 200, body: {} } });
    sdk.callAction('notes_add');
    await vi.waitFor(() => expect(panel.postMessage).toHaveBeenCalledTimes(2 + MAX_IN_FLIGHT));
  });

  it('bounds the calls that wait for the handshake too', async () => {
    const { panel, app, sdk } = setup();
    for (let i = 0; i < MAX_IN_FLIGHT; i++) sdk.callAction('notes_add');
    expect(await sdk.callAction('notes_add')).toEqual({ status: 429, body: null });
    await app.deliver(hello(panel));
    await vi.waitFor(() => expect(panel.postMessage).toHaveBeenCalledTimes(1 + MAX_IN_FLIGHT));
  });

  it.each(['*', `${PANEL}/`, 'http://panel.example.com', undefined])('refuses panelOrigin %s', (panelOrigin) => {
    expect(() => createAgentoSdk({ panelOrigin, window: fakeWindow(fakeWindow()) })).toThrow(/panelOrigin/);
  });

  it('refuses to start without an opener', () => {
    expect(() => createAgentoSdk({ panelOrigin: PANEL, window: fakeWindow(null) })).toThrow(/opened by the Agento panel/);
  });
});
