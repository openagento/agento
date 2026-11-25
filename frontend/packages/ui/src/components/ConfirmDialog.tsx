// Mantine's Modal gives the focus trap, Esc and focus return; the content is `.ag-*`.
import { Modal } from "@mantine/core";
import type { ReactNode } from "react";
import { Button } from "./Button";
import { useT } from "../i18n";

/** `cancelLabel={null}`: one closing button, for a dialog whose changes apply as made (Share). */
export function ConfirmDialog({ opened, title, children, confirmLabel, cancelLabel, danger = false,
  busy = false, onConfirm, onCancel }: {
  opened: boolean; title: string; children?: ReactNode; confirmLabel?: string; cancelLabel?: string | null; danger?: boolean;
  busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const t = useT("ui");
  return (
    <Modal opened={opened} onClose={onCancel} title={title} centered closeButtonProps={{ "aria-label": t("action.close") }}>
      <div className="ag-stack">
        {children}
        <div className="ag-row">
          {cancelLabel !== null && <Button onClick={onCancel}>{cancelLabel ?? t("action.cancel")}</Button>}
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>{confirmLabel ?? t("action.confirm")}</Button>
        </div>
      </div>
    </Modal>
  );
}
