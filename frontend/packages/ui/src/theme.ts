// The Mantine theme is built from the kit's token file (PRD E8 §10.3), so a Mantine
// widget in the panel and an `.ag-*` element in a miniapp use the same values.
import { createTheme, type MantineColorsTuple } from "@mantine/core";
import { agentoScale, font, radius } from "@agento/miniapp-kit/tokens";

export const agentoTheme = createTheme({
  primaryColor: "agento",
  primaryShade: { light: 7, dark: 3 },
  colors: { agento: [...agentoScale] as unknown as MantineColorsTuple },
  fontFamily: font.family,
  fontFamilyMonospace: font.mono,
  headings: { fontFamily: font.family },
  defaultRadius: "md",
  radius: { xs: radius.sm, sm: radius.sm, md: radius.md, lg: radius.lg, xl: radius.lg },
});
