import { QueryClient } from "@tanstack/react-query";
import { ApiError, SessionChangedError } from "./errors";
import { onEndSession } from "./session";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // A 4xx will not change on retry; a dropped late answer is not a failure to retry.
      retry: (count, e) => !(e instanceof SessionChangedError)
        && !(e instanceof ApiError && e.status < 500) && count < 2,
    },
    mutations: { retry: false },
  },
});

// A new user must never see the previous user's data (SEC-7).
onEndSession(() => {
  void queryClient.cancelQueries();
  queryClient.clear();
});
