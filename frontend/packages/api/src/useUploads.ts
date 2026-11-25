// Staged files (framework/uploads.py): one upload per picked file, and the ids a later request
// then claims. The conversation composer and the schedule form share it; each passes its own
// staging route.
import { useLayoutEffect, useRef, useState } from "react";
import { apiFetch } from "./apiFetch";
import { useErrorText } from "./errorText";

/** One file the form is carrying. Structurally an `@agento/ui` `Attachment`. */
export interface StagedFile {
  id: string; name: string; status: "uploading" | "ready" | "error"; error?: string; bytes?: number; uploadId?: string;
}

/** A file is staged the moment it is picked, so the request that uses it is a normal JSON post
 *  that only names ids. A failed upload stays in the list as an `error` entry until the user
 *  removes it: it is never dropped without a word, and it is never in `ids`.
 *
 *  `endpoint` is the POST route that stages one file. `initial` are the files already there.
 *  `namespace` is the catalog that holds the route's own `error.<CODE>` texts. */
interface HeldFile extends Omit<StagedFile, "error"> { failure?: unknown }

export function useUploads(endpoint: string, initial: StagedFile[] = [], namespace = "panel") {
  const errorText = useErrorText(namespace);
  // The endpoint is part of the state, not an effect that clears it: a change of owner must
  // not carry a picked file to another one, and a reset in an effect would show the old list
  // for one frame.
  const [held, setHeld] = useState<{ endpoint: string; list: HeldFile[] }>({ endpoint, list: initial });
  const files: StagedFile[] = (held.endpoint === endpoint ? held.list : []).map(({ failure, ...file }) =>
    (file.status === "error" ? { ...file, error: errorText(failure, "error.UPLOAD_FAILED") } : file));
  // An upload answers late. Its callback carries the endpoint it started with, and a write for
  // an endpoint the user has left is dropped: it must not put that list over the open one.
  const shown = useRef(endpoint);
  useLayoutEffect(() => { shown.current = endpoint; }, [endpoint]);
  const setFiles = (next: (list: HeldFile[]) => HeldFile[]) => {
    if (shown.current !== endpoint) return;
    setHeld((prev) => ({ endpoint, list: next(prev.endpoint === endpoint ? prev.list : []) }));
  };
  const patch = (id: string, change: Partial<HeldFile>) =>
    setFiles((list) => list.map((f) => (f.id === id ? { ...f, ...change } : f)));
  const add = (picked: File[]) => {
    for (const file of picked) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setFiles((list) => [...list, { id, name: file.name, bytes: file.size, status: "uploading" }]);
      const form = new FormData();
      form.append("file", file, file.name);
      apiFetch<{ upload_id: string }>(endpoint, { method: "POST", form })
        .then((res) => patch(id, { status: "ready", uploadId: res.upload_id }))
        .catch((err: unknown) => patch(id, { status: "error", failure: err }));
    }
  };
  return {
    files, add,
    remove: (id: string) => setFiles((list) => list.filter((f) => f.id !== id)),
    /** Replace the list: with nothing after a send, with the saved files after a discard. */
    reset: (list: StagedFile[] = []) => setFiles(() => list),
    /** The staged ids, in order: what the request names. */
    ids: files.flatMap((f) => (f.status === "ready" && f.uploadId ? [f.uploadId] : [])),
    busy: files.some((f) => f.status === "uploading"),
  };
}
