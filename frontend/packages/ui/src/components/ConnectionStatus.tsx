import { Button } from "./Button";
import { StatusBadge, type BadgeTone } from "./StatusBadge";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "refused";

const VIEW: Record<ConnectionState, [BadgeTone, string]> = {
  connecting: ["pending", "Connecting"],
  live: ["succeeded", "Live"],
  reconnecting: ["blocked", "Reconnecting"],
  refused: ["failed", "Disconnected"],
};

export function ConnectionStatus({ state, onReconnect }: { state: ConnectionState; onReconnect?: () => void }) {
  const [tone, label] = VIEW[state];
  return (
    <span className="ag-row" role="status">
      <StatusBadge tone={tone}>{label}</StatusBadge>
      {state === "refused" && onReconnect && <Button variant="subtle" onClick={onReconnect}>Reconnect</Button>}
    </span>
  );
}
