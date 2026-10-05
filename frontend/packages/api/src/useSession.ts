import { useSyncExternalStore } from "react";
import { getDisplay, getUser, subscribeUser, type Display, type User } from "./session";

export function useSession(): User | null {
  return useSyncExternalStore(subscribeUser, getUser, getUser);
}

export function useDisplay(): Display | null {
  return useSyncExternalStore(subscribeUser, getDisplay, getDisplay);
}
