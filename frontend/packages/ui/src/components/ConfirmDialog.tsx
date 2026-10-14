// Mantine's Modal gives the focus trap, Esc and focus return; the content is `.ag-*`.
import { Modal } from "@mantine/core";
import type { ReactNode } from "react";
import { Button } from "./Button";

export function ConfirmDialog({ opened, title, children, confirmLabel = "Confirm", danger = false, busy = false,
  onConfirm, onCancel }: {
  opened: boolean; title: string; children?: ReactNode; confirmLabel?: string; danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal opened={opened} onClose={onCancel} title={title} centered closeButtonProps={{ "aria-label": "Close" }}>
      <div className="ag-stack">
        {children}
        <div className="ag-row">
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>{confirmLabel}</Button>
        </div>
      </div>
    </Modal>
  );
}
