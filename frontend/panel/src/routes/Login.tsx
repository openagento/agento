import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { errorCode, fetchBootstrap, getLocale, login } from "@agento/api";
import { Button, FormSection, I18nProvider, TextField, useDocumentTitle, useLookup, useT, type Catalog } from "@agento/ui";
import { BUNDLED_ENGLISH, ensureCatalog, withDeadline } from "../catalogStore";
import { useScreenLanguage } from "../I18n";
import { safeNext } from "../safeNext";

/** The sign-in texts, once they are settled: the instance language, or the bundled English when
 *  the read fails or takes too long. They are public and belong to this screen only: the store
 *  of the session's catalog never gets them, so a signed-in user who opens this screen and goes
 *  back finds the account's language as it was. */
function useLoginCatalog(): Catalog | null {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  useEffect(() => {
    let current = true;
    withDeadline(fetchBootstrap)
      .then((served): Catalog => ({ locale: served.locale, messages: served.messages, fallback: served.fallback ?? {} }))
      .catch(() => BUNDLED_ENGLISH)
      .then((texts) => { if (current) setCatalog(texts); });
    return () => { current = false; };
  }, []);
  useScreenLanguage(catalog?.locale ?? null);
  return catalog;
}

/** The form waits for its texts, so it never shows two languages. */
export function Login() {
  const catalog = useLoginCatalog();
  if (!catalog) return <main className="ag-page" aria-busy />;
  return <I18nProvider locale={catalog.locale} messages={catalog.messages} fallback={catalog.fallback}><LoginForm /></I18nProvider>;
}

function LoginForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState<{ code: string | undefined } | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const t = useT("panel");
  const lookup = useLookup();
  useDocumentTitle(t("login.document_title"));

  const submit = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await login(username, password);
      await ensureCatalog(getLocale());
      navigate(safeNext(params.get("next")), { replace: true });
    } catch (e) {
      setFailure({ code: errorCode(e) });
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  const error = failure && ((failure.code && lookup("panel", `login.error.${failure.code}`)) || t("login.error.generic"));
  return (
    <main className="ag-page" style={{ maxWidth: 400 }}>
      <FormSection title={t("login.title")} onSubmit={submit} error={error}>
        <TextField label={t("login.username")} name="username" autoComplete="username" required value={username}
          onChange={(e) => setUsername(e.target.value)} />
        <TextField label={t("login.password")} name="password" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} />
        <div className="ag-row"><Button type="submit" variant="primary" disabled={busy}>{t("login.submit")}</Button></div>
      </FormSection>
    </main>
  );
}
