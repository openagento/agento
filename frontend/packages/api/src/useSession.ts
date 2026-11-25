import { useSyncExternalStore } from "react";
import { getDisplay, getLocale, getUser, subscribeUser, type Display, type SessionLocale, type User } from "./session";

export function useSession(): User | null {
  return useSyncExternalStore(subscribeUser, getUser, getUser);
}

/** Whether the user holds an admin leaf (`admin.jobs`, `miniapps`, …). Display only: the API checks it too. */
export const useCan = (leaf: string): boolean => useSession()?.acl.includes(leaf) ?? false;

export function useDisplay(): Display | null {
  return useSyncExternalStore(subscribeUser, getDisplay, getDisplay);
}

/** The session's language data; null before sign-in and for a server that sends none. */
export function useSessionLocale(): SessionLocale | null {
  return useSyncExternalStore(subscribeUser, getLocale, getLocale);
}
