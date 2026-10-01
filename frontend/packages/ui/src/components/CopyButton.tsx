import { useState } from "react";
import { Button } from "./Button";

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [status, setStatus] = useState("");
  return (
    <>
      <Button variant="subtle" onClick={async () => {
        try { await navigator.clipboard.writeText(value); setStatus("Copied"); }
        catch { setStatus("Copy failed"); }
      }}>{label}</Button>
      <span className="ag-visually-hidden" role="status">{status}</span>
    </>
  );
}
