import { Notifications } from "@mantine/notifications";
import { QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { RouterProvider } from "react-router";
import { router } from "./router";

export function App() {
  return (
    <AgentoUiProvider defaultColorScheme="auto">
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </AgentoUiProvider>
  );
}
