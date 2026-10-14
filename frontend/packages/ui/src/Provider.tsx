import { MantineProvider, type MantineProviderProps } from "@mantine/core";
import { agentoCssVariables, agentoTheme } from "./theme";

/** MantineProvider with the Agento theme. Every @agento/ui component needs it above it. */
export function AgentoUiProvider(props: Omit<MantineProviderProps, "theme" | "cssVariablesResolver">) {
  return <MantineProvider theme={agentoTheme} cssVariablesResolver={agentoCssVariables} {...props} />;
}
