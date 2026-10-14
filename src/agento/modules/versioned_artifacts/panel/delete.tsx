// The artifact delete, used by both screens (the list row menu and the artifact page): the DELETE
// with its typed-code confirm. One copy, so the two screens cannot drift apart.
// The hook takes the code at `open(code)`, not at call time: a row's menu item is a cell, and a hook
// per row would mean a component per cell (UI-6).
import { useState, type ReactNode } from "react";
import { apiFetch, useMutation, useQueryClient } from "@agento/api";
import { ConfirmDialog, TextField } from "@agento/ui";
import { BASE, message } from "./api";

export function useDeleteArtifact(onDone?: (code: string) => void): {
  open: (code: string) => void; dialog: ReactNode; failure: string | null;
} {
  const qc = useQueryClient();
  const [code, setCode] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: (target: string) => apiFetch(`${BASE}/${encodeURIComponent(target)}`, { method: "DELETE" }),
    onSuccess: async (_r, target) => {
      setCode(null);
      await qc.invalidateQueries({ queryKey: ["artifacts"] });
      onDone?.(target);
    },
    onError: (e) => { setCode(null); setFailure(message(e)); },
  });
  return {
    open: (next: string) => { setTyped(""); setFailure(null); setCode(next); },
    failure,
    dialog: (
      <ConfirmDialog opened={code !== null} title="Delete artifact" confirmLabel="Delete artifact" danger
        busy={typed !== code || remove.isPending} onConfirm={() => code && remove.mutate(code)} onCancel={() => setCode(null)}>
        <p>This removes every version, the published files, the share and every approval. Open launches end. It cannot be undone.</p>
        <TextField label="Type the code to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      </ConfirmDialog>
    ),
  };
}
