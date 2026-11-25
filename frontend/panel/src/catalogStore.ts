// The catalog the panel shows. It lives outside React because the router loader, which runs
// before any provider mounts, fills it (`ensureCatalog`).
import {
  errorCode, fetchCatalog, fetchCatalogUnless, onEndSession, saveLocale, within, type ServedCatalog, type SessionLocale,
} from "@agento/api";
import { SOURCE_LOCALE, type Catalog } from "@agento/ui";

export const CATALOG_DEADLINE_MS = 2000;

/** `version` is null for the bundled English only: it is not a served catalog, so it never
 *  counts as "already loaded". The store holds the session's catalog, or the bundled English. */
export interface HeldCatalog extends Catalog { version: string | null }
interface WholeCatalog extends HeldCatalog { version: string }

export const BUNDLED_ENGLISH: HeldCatalog = { locale: SOURCE_LOCALE, version: null, messages: {}, fallback: {} };

/** What one session may keep: the newest whole catalog it read for each language. Only a
 *  session may read one, so the end of the session puts a new, empty object in `current`. */
interface Session { whole: Map<string, WholeCatalog> }
let current: Session = { whole: new Map() };

let held = BUNDLED_ENGLISH;
const listeners = new Set<() => void>();
/** The start order of the shows. A read shows its catalog only when no show that started
 *  later is on the screen (`shownAt`), so the newest request wins in any order of the answers. */
let lastShow = 0;
let shownAt = 0;

/** The one language change of this session: the language a save is storing now (`pending`), or
 *  the last change that failed. It lives here, not in the form: the form can close and open
 *  again while the save runs. */
export interface LanguageSwitch { pending: string | null; failed: { tag: string; error: unknown } | null }
const NO_SWITCH: LanguageSwitch = { pending: null, failed: null };
let languageSwitch = NO_SWITCH;

onEndSession(() => {
  current = { whole: new Map() };
  languageSwitch = NO_SWITCH;
  hold(BUNDLED_ENGLISH);
});

class SessionEnded extends Error {}

/** The one door to everything outside the session's own object: the shown catalog, the language
 *  change and every request. It checks the session and runs `effect` in the same call, with no
 *  `await` between, so the end of a session cannot come between the check and the effect. An
 *  operation takes `current` once, at its start; its copies go to that object, which nothing
 *  reads after the session ended. `catalogStore.interleaving.test.ts` holds the rule. */
function live<T>(session: Session, effect: () => T): T {
  if (session !== current) throw new SessionEnded();
  return effect();
}

/** An effect of a failure path: it does nothing when the session ended. */
function liveOrNothing(session: Session, effect: () => void): void {
  try {
    live(session, effect);
  } catch (error) {
    if (!(error instanceof SessionEnded)) throw error;
  }
}

export const getCatalog = () => held;

export function subscribeCatalog(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

const tell = () => listeners.forEach((l) => l());

function newShow(): number {
  lastShow += 1;
  return lastShow;
}

function hold(next: HeldCatalog, startedAt = newShow()): void {
  held = next;
  shownAt = startedAt;
  tell();
}

function holdUnlessNewer(next: HeldCatalog, startedAt: number): void {
  if (startedAt > shownAt) hold(next, startedAt);
}

export const getLanguageSwitch = () => languageSwitch;

function note(next: LanguageSwitch): void {
  languageSwitch = next;
  tell();
}

export function forgetFailedSwitch(): void {
  if (languageSwitch.failed) note({ ...languageSwitch, failed: null });
}

const whole = (served: ServedCatalog): WholeCatalog =>
  ({ locale: served.locale, version: served.version, messages: served.messages, fallback: served.fallback ?? {} });

export const withDeadline = <T>(read: (signal: AbortSignal) => Promise<T>) => within(CATALOG_DEADLINE_MS, read);

function keep(session: Session, served: ServedCatalog): WholeCatalog {
  const catalog = whole(served);
  session.whole.set(catalog.locale, catalog);
  return catalog;
}

async function read(session: Session, tag: string): Promise<WholeCatalog> {
  return keep(session, await live(session, () => withDeadline((signal) => fetchCatalog(tag, signal))));
}

/** The catalog of `tag` at the version the server has now: the session's copy when the server
 *  says that it did not change, else a new read. */
async function readCurrent(session: Session, tag: string): Promise<WholeCatalog> {
  const copy = session.whole.get(tag);
  if (!copy) return read(session, tag);
  const changed = await live(session, () => withDeadline((signal) => fetchCatalogUnless(tag, copy.version, signal)));
  return changed ? keep(session, changed) : copy;
}

/** True when the screen already shows the catalog of `locale`: that counts as a show. */
function shows(locale: SessionLocale, startedAt: number): boolean {
  const shown = held.locale === locale.effective && held.version === locale.version;
  if (shown) shownAt = startedAt;
  return shown;
}

async function showCatalogOf(session: Session, locale: SessionLocale): Promise<void> {
  const startedAt = live(session, newShow);
  if (live(session, () => shows(locale, startedAt))) return;
  try {
    const copy = session.whole.get(locale.effective);
    const catalog = copy?.version === locale.version ? copy : await read(session, locale.effective);
    live(session, () => holdUnlessNewer(catalog, startedAt));
  } catch {
    liveOrNothing(session, () => holdUnlessNewer(BUNDLED_ENGLISH, startedAt));
  }
}

/** Shows the catalog of the session's language: the copy this session already read when the
 *  server reports the same version, else a new read. A failure shows the bundled English; the
 *  next session start tries again. A read that an ended session started changes nothing, and
 *  so does a read that answers after a newer show. */
export async function ensureCatalog(locale: SessionLocale | null): Promise<void> {
  const session = current;
  if (locale) await showCatalogOf(session, locale);
}

const catalogChanged = (error: unknown) => errorCode(error) === "CATALOG_CHANGED";

/** Reads the catalog of `tag` and saves `tag` with the version that was read. The server refuses
 *  (`CATALOG_CHANGED`) when its catalog is another version: the copy is then dropped, so the next
 *  read is a whole one. */
async function readAndSave(session: Session, tag: string): Promise<{ catalog: WholeCatalog; saved: SessionLocale }> {
  const catalog = await readCurrent(session, tag);
  try {
    return { catalog, saved: await live(session, () => saveLocale(tag, catalog.version)) };
  } catch (error) {
    if (catalogChanged(error)) [tag, catalog.locale].forEach((dropped) => session.whole.delete(dropped));
    throw error;
  }
}

async function readAndSaveAgainIfChanged(session: Session, tag: string) {
  try {
    return await readAndSave(session, tag);
  } catch (error) {
    if (!catalogChanged(error)) throw error;
    return await readAndSave(session, tag);
  }
}

/** Stores `tag` as the account's language and shows it; true when it did. The catalog comes
 *  first, at the version the server has now, and the save second, with that version: a failure
 *  of either step leaves the language, the session and the screen as they are, and is kept as
 *  `failed`. A catalog that changed between the read and the save is read again, once. Only
 *  one change runs at a time: a call during a pending one starts nothing. A change that an
 *  ended session started changes nothing. When the server stored `tag` but shows another
 *  language (`tag` is no longer installed), the screen shows that language: false. */
export async function switchLanguage(tag: string): Promise<boolean> {
  const session = current;
  const started = live(session, () => {
    if (languageSwitch.pending !== null) return false;
    note({ pending: tag, failed: null });
    return true;
  });
  if (!started) return false;
  try {
    const { catalog, saved } = await readAndSaveAgainIfChanged(session, tag);
    const confirmed = saved.effective === catalog.locale && saved.version === catalog.version;
    if (!confirmed) await showCatalogOf(session, saved);
    return live(session, () => {
      languageSwitch = NO_SWITCH;
      if (confirmed) hold(catalog);
      else tell();
      return held.locale === tag;
    });
  } catch (error) {
    liveOrNothing(session, () => note({ pending: null, failed: { tag, error } }));
    return false;
  }
}
