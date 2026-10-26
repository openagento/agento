import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { ApiError, login } from "@agento/api";
import { Button, FormSection, TextField } from "@agento/ui";
import { safeNext } from "../safeNext";

export function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      navigate(safeNext(params.get("next")), { replace: true });
    } catch (e) {
      setError(e instanceof ApiError && e.status === 429
        ? "Too many failed sign-ins. Try again later."
        : e instanceof ApiError && e.status === 401 ? "The user name or password is wrong." : "Sign-in failed.");
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <main className="ag-page" style={{ maxWidth: 400 }}>
      <FormSection title="Sign in to Agento" onSubmit={submit} error={error}>
        <TextField label="User name" name="username" autoComplete="username" required value={username}
          onChange={(e) => setUsername(e.target.value)} />
        <TextField label="Password" name="password" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} />
        <div className="ag-row"><Button type="submit" variant="primary" disabled={busy}>Sign in</Button></div>
      </FormSection>
    </main>
  );
}
