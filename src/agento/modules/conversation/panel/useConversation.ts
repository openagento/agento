// The messages of one thread: REST is the only source of persisted rows; the stream only
// says "refetch" (debounced) and feeds the bounded transient store (PRD E8 §9).
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  apiFetch, hub, PERSISTED_KINDS, useQuery, useQueryClient, type StreamState,
} from "@agento/api";
import { IDLE_POLL_MS, inFlight, RUN_POLL_MS, type Message } from "./model";
import { TransientStore } from "./transient";

export const REFETCH_DEBOUNCE_MS = 250;
const persisted = new Set<string>(PERSISTED_KINDS);

export const messagesKey = (threadId: number) => ["messages", threadId] as const;

export function useConversation(threadId: number) {
  const qc = useQueryClient();
  // One store per mounted view; the page keys <View> by thread, so a new thread is a new store.
  const [store] = useState(() => new TransientStore());
  const [stream, setStream] = useState<StreamState>("connecting");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const messages = useQuery({
    queryKey: messagesKey(threadId),
    queryFn: ({ signal }) => apiFetch<Message[]>(`/api/conversation/threads/${threadId}/messages`, { signal }),
    // Always polled while on screen: the stream alone can miss an event (PRD E8 §9).
    refetchInterval: (q) => (inFlight(q.state.data) ? RUN_POLL_MS : IDLE_POLL_MS),
    refetchIntervalInBackground: false,
  });

  // A reconcile that shows no run in flight ends every transient row, even if the event
  // that would have ended it was missed.
  useEffect(() => {
    if (messages.data && !inFlight(messages.data)) store.clear();
  }, [messages.data, messages.dataUpdatedAt, store]);

  useEffect(() => {
    const refetch = () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        void qc.invalidateQueries({ queryKey: messagesKey(threadId) });
      }, REFETCH_DEBOUNCE_MS);
    };
    hub.open(threadId, {
      onState: setStream,
      onResync: refetch,
      onEvent: (e) => {
        if (persisted.has(e.kind)) refetch();
        else store.apply(e);
      },
    });
    return () => {
      hub.close();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [threadId, qc, store]);

  useSyncExternalStore(store.subscribe, store.getVersion);
  return { messages, store, stream, reconnect: () => hub.reconnect() };
}
