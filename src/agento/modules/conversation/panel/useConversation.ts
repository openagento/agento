// One thread on screen (E9 §3.8). The timeline store holds every event: the newest page first,
// then the stream from that page's newest id, so there is no window between them. The messages
// (turn state, Unblock) and the runs stay REST rows; a persisted kind refetches them (debounced).
// The REST poll is the fallback when the stream is down: each messages poll also pages the
// timeline forward through the replay route, and the runs poll on the same clock.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { apiFetch, ApiError, hub, useQuery, useQueryClient, type StreamEvent, type StreamState } from "@agento/api";
import { IDLE_POLL_MS, inFlight, RUN_POLL_MS, type Message, type ThreadDetail, type TimelinePage } from "./model";
import { TimelineStore } from "./timeline";

export const REFETCH_DEBOUNCE_MS = 250;
const refetches = (kind: string) =>
  kind === "message.created" || kind === "assistant.message" || kind.startsWith("job.") || kind.startsWith("run.");

export const messagesKey = (threadId: number) => ["messages", threadId] as const;
export const threadKey = (threadId: number) => ["thread", threadId] as const;

/** The newest timeline page, or the page before event `before`. */
export const fetchPage = (threadId: number, before?: number, signal?: AbortSignal) =>
  apiFetch<TimelinePage>(`/api/conversation/threads/${threadId}/timeline${before === undefined ? "" : `?before=${before}`}`, { signal });

/** Resets the store to the newest page and the replay cursor to its newest id: the first load,
 *  an expired cursor (409) and following again all start here. A page that arrives after the
 *  operator scrolled up changes nothing: the history being read stays. */
async function reload(threadId: number, store: TimelineStore, cursor: { current: number }, signal?: AbortSignal): Promise<TimelinePage> {
  const generation = store.generation;
  const page = await fetchPage(threadId, undefined, signal);
  if (store.behind || store.generation !== generation) return page;
  store.reset(page);
  cursor.current = page.newest_id ?? 0;
  return page;
}

/** Pages forward from `cursor`: the highest id up to which the client holds every event. Only
 *  a replay page (or a reload) moves it; a stream frame, a reconnect page or an older page never
 *  does, since each can leave a hole below it. An empty page ends the walk; so does scrolling up,
 *  since the store then holds no `auto` events and following again reloads. */
async function catchUp(threadId: number, store: TimelineStore, cursor: { current: number }, signal: AbortSignal): Promise<void> {
  try {
    while (!store.behind) {
      const page = await apiFetch<StreamEvent[]>(`/api/conversation/threads/${threadId}/events?after=${cursor.current}`, { signal });
      if (!page.length || store.behind) return;
      store.merge("auto", page);
      cursor.current = Math.max(cursor.current, ...page.map((e) => e.id));
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) await reload(threadId, store, cursor, signal);
  }
}

export function useConversation(threadId: number) {
  const qc = useQueryClient();
  // One store per mounted view; the page keys <View> by thread, so a new thread is a new store.
  const [store] = useState(() => new TimelineStore());
  const [stream, setStream] = useState<StreamState>("connecting");
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursor = useRef(0);

  const messages = useQuery({
    queryKey: messagesKey(threadId),
    queryFn: ({ signal }) => apiFetch<Message[]>(`/api/conversation/threads/${threadId}/messages`, { signal }),
    // Always polled while on screen: the stream alone can miss an event (PRD E8 §9).
    refetchInterval: (q) => (inFlight(q.state.data) ? RUN_POLL_MS : IDLE_POLL_MS),
    refetchIntervalInBackground: false,
  });
  const thread = useQuery({
    queryKey: threadKey(threadId),
    queryFn: ({ signal }) => apiFetch<ThreadDetail>(`/api/conversation/threads/${threadId}`, { signal }),
    refetchInterval: () => (inFlight(qc.getQueryData<Message[]>(messagesKey(threadId))) ? RUN_POLL_MS : IDLE_POLL_MS),
    refetchIntervalInBackground: false,
  });

  // Each messages fetch (the poll tick) also pages the timeline forward.
  const tick = messages.dataUpdatedAt;
  useEffect(() => {
    if (!loaded || !tick) return;
    const ctrl = new AbortController();
    catchUp(threadId, store, cursor, ctrl.signal).catch(() => {});
    return () => ctrl.abort();
  }, [threadId, store, loaded, tick]);

  useEffect(() => {
    const ctrl = new AbortController();
    const refetch = () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        void qc.invalidateQueries({ queryKey: messagesKey(threadId) });
        void qc.invalidateQueries({ queryKey: threadKey(threadId) });
      }, REFETCH_DEBOUNCE_MS);
    };
    let opened = false;
    reload(threadId, store, cursor, ctrl.signal).then((page) => {
      setLoaded(true);
      hub.open(threadId, {
        onState: setStream,
        onResync: () => {
          refetch();
          // A reopen after an expired cursor starts at the newest event: the newest page heals the gap.
          if (opened) fetchPage(threadId, undefined, ctrl.signal).then((p) => store.merge("auto", p.events), () => {});
          opened = true;
        },
        onEvent: (e) => {
          store.merge("auto", [e]);
          if (refetches(e.kind)) refetch();
        },
      }, page.newest_id ?? 0);
    }, (e) => { if (!ctrl.signal.aborted) setLoadError(e); });
    return () => {
      ctrl.abort();
      hub.close();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [threadId, qc, store, attempt]);

  const version = useSyncExternalStore(store.subscribe, store.getVersion);
  return {
    messages, thread, store, version, stream, loaded, loadError,
    retry: () => { setLoadError(null); setAttempt((a) => a + 1); },
    reconnect: () => hub.reconnect(),
    /** Follows live output again: events held back while scrolled up come from the newest page. */
    resume: () => { store.follow(true); reload(threadId, store, cursor).catch(() => {}); },
  };
}
