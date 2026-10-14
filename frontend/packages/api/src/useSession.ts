import { useSyncExternalStore } from "react";
import { getDisplay, getUser, subscribeUser, type Display, type User } from "./session";

export function useSession(): User | null {
  return useSyncExternalStore(subscribeUser, getUser, getUser);
}

/** Whether the user holds an admin leaf (`admin.jobs`, `miniapps`, …). Display only: the API checks it too. */
export const useCan = (leaf: string): boolean => useSession()?.acl.includes(leaf) ?? false;

export function useDisplay(): Display | null {
  return useSyncExternalStore(subscribeUser, getDisplay, getDisplay);
}
