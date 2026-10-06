import { CopyButton as MantineCopyButton } from "@mantine/core";
import { Check, Copy } from "lucide-react";
import { IconAction } from "./IconAction";

/** Copies `value`. An icon whose title says "Copied" for a moment after the click. */
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  return (
    <MantineCopyButton value={value} timeout={1500}>
      {({ copied, copy }) => (
        <IconAction label={copied ? "Copied" : label} onClick={copy}
          icon={copied ? <Check size={16} /> : <Copy size={16} />} />
      )}
    </MantineCopyButton>
  );
}
