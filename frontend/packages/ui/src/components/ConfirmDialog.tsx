// Mantine's Modal gives the focus trap, Esc and focus return; the content is `.ag-*`.
import { Modal } from "@mantine/core";
import type { ReactNode } from "react";
import { Button } from "./Button";

/** `cancelLabel={null}`: one closing button, for a dialog whose changes apply as made (Share). */
export function ConfirmDialog({ opened, title, children, confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false,
  busy = false, onConfirm, onCancel }: {
  opened: boolean; title: string; children?: ReactNode; confirmLabel?: string; cancelLabel?: string | null; danger?: boolean;
  busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal opened={opened} onClose={onCancel} title={title} centered closeButtonProps={{ "aria-label": "Close" }}>
      <div className="ag-stack">
        {children}
        <div className="ag-row">
          {cancelLabel !== null && <Button onClick={onCancel}>{cancelLabel}</Button>}
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>{confirmLabel}</Button>
        </div>
      </div>
    </Modal>
  );
}
