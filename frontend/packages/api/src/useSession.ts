import { useSyncExternalStore } from "react";
import { getUser, subscribeUser, type User } from "./session";

export function useSession(): User | null {
  return useSyncExternalStore(subscribeUser, getUser, getUser);
}
