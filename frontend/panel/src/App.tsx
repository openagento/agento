import { Notifications } from "@mantine/notifications";
import { PanelModulesProvider, QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { RouterProvider } from "react-router";
import { router } from "./router";
import { SessionDisplayFormat } from "./DisplayFormat";
import { SessionI18n } from "./I18n";
import { MODULES } from "./generated/registry";

export function App() {
  return (
    <AgentoUiProvider defaultColorScheme="auto">
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <SessionI18n>
          <SessionDisplayFormat>
            <PanelModulesProvider modules={MODULES}>
              <RouterProvider router={router} />
            </PanelModulesProvider>
          </SessionDisplayFormat>
        </SessionI18n>
      </QueryClientProvider>
    </AgentoUiProvider>
  );
}
