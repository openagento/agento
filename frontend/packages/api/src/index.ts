export { apiFetch, sameOriginUrl, type ApiOptions } from "./apiFetch";
export { ApiError, CsrfMissingError, SessionChangedError } from "./errors";
export {
  boot, login, logout, endSession, onEndSession, onExpired, getUser, subscribeUser, type User,
} from "./session";
export { queryClient } from "./query";
export {
  hub, EventSourceHub, backoff, PERSISTED_KINDS, TRANSIENT_KINDS,
  type StreamEvent, type StreamHandlers, type StreamState,
} from "./hub";
export { useSession } from "./useSession";
// Modules import Query through this package only (one QueryClient, one teardown).
export { useQuery, useMutation, useQueryClient, QueryClientProvider } from "@tanstack/react-query";
export {
  assertPanelModule, PANEL_CONTRACT_VERSION, RegistryContractError, type PanelModule, type PanelRoute,
} from "./contract";
