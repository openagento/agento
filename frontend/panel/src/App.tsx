import { Notifications } from "@mantine/notifications";
import { QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { RouterProvider } from "react-router";
import { router } from "./router";
import { SessionDisplayFormat } from "./DisplayFormat";

export function App() {
  return (
    <AgentoUiProvider defaultColorScheme="auto">
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <SessionDisplayFormat>
          <RouterProvider router={router} />
        </SessionDisplayFormat>
      </QueryClientProvider>
    </AgentoUiProvider>
  );
}
