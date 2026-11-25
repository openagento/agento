import { useT, type Translate } from "../i18n";
import { Button } from "./Button";
import { StatusBadge, type BadgeTone } from "./StatusBadge";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "refused";

const view = (t: Translate): Record<ConnectionState, [BadgeTone, string]> => ({
  connecting: ["pending", t("connection.connecting")],
  live: ["succeeded", t("connection.live")],
  reconnecting: ["blocked", t("connection.reconnecting")],
  refused: ["failed", t("connection.refused")],
});

export function ConnectionStatus({ state, onReconnect }: { state: ConnectionState; onReconnect?: () => void }) {
  const t = useT("ui");
  const [tone, label] = view(t)[state];
  return (
    <span className="ag-row" role="status">
      <StatusBadge tone={tone}>{label}</StatusBadge>
      {state === "refused" && onReconnect && <Button variant="subtle" onClick={onReconnect}>{t("connection.reconnect")}</Button>}
    </span>
  );
}
