import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClientProvider, queryClient } from "@agento/api";
import { agentoTheme } from "@agento/ui";
import { RouterProvider } from "react-router";
import { router } from "./router";

export function App() {
  return (
    <MantineProvider theme={agentoTheme} defaultColorScheme="auto">
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </MantineProvider>
  );
}
