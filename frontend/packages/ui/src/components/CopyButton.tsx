import { CopyButton as MantineCopyButton } from "@mantine/core";
import { Check, Copy } from "lucide-react";
import { IconAction } from "./IconAction";
import { useT } from "../i18n";

/** Copies `value`. An icon whose title says "Copied" for a moment after the click. */
export function CopyButton({ value, label }: { value: string; label?: string }) {
  const t = useT("ui");
  return (
    <MantineCopyButton value={value} timeout={1500}>
      {({ copied, copy }) => (
        <IconAction label={copied ? t("action.copied") : label ?? t("action.copy")} onClick={copy}
          icon={copied ? <Check size={16} /> : <Copy size={16} />} />
      )}
    </MantineCopyButton>
  );
}
