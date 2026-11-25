import { useState, useSyncExternalStore } from "react";
import { Alert, Modal } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useErrorText, useSession, useSessionLocale } from "@agento/api";
import { Button, FormSection, SelectField, translate, useLocale, useT } from "@agento/ui";
import { forgetFailedSwitch, getCatalog, getLanguageSwitch, subscribeCatalog, switchLanguage } from "./catalogStore";

const savedTextInNewLanguage = (language: string) =>
  translate(getCatalog(), "panel", "profile.language.saved", { language });

function LanguageForm({ onClose }: { onClose: () => void }) {
  const user = useSession();
  const locale = useSessionLocale();
  const t = useT("panel");
  const shown = useLocale();
  const errorText = useErrorText("panel");
  const [picked, setPicked] = useState<string | null>(null);
  const { pending, failed } = useSyncExternalStore(subscribeCatalog, getLanguageSwitch, getLanguageSwitch);
  if (!user || !locale) return null;

  const nameOf = (tag: string) => locale.available.find((l) => l.tag === tag)?.name ?? tag;
  const stored = user.ui_locale;
  const storedIsMissing = stored !== null && !locale.available.some((l) => l.tag === stored);
  const saving = pending !== null;
  const choice = pending ?? picked ?? failed?.tag ?? locale.effective;
  const nothingToSave = choice === stored && choice === shown;

  const save = async () => {
    if (!await switchLanguage(choice)) return;
    setPicked(null);
    onClose();
    notifications.show({ message: savedTextInNewLanguage(nameOf(choice)) });
  };

  return (
    <div className="ag-stack">
      {storedIsMissing && (
        <Alert color="yellow" title={t("profile.language.unavailable_title")}>
          {t("profile.language.unavailable", { stored, active: nameOf(locale.effective) })}
        </Alert>
      )}
      <FormSection title={t("profile.language.section")} onSubmit={() => void save()}
        error={failed ? errorText(failed.error, "profile.language.save_failed") : null}>
        <SelectField label={t("profile.language.label")} hideLabel name="ui_locale" hint={t("profile.language.hint")}
          options={locale.available.map((l) => ({ value: l.tag, label: l.name }))} value={choice} onChange={setPicked}
          disabled={saving} />
        {saving && <p className="ag-muted" role="status">{t("profile.language.saving", { language: nameOf(choice) })}</p>}
        <div className="ag-row">
          <Button onClick={onClose}>{t("action.close")}</Button>
          <Button type="submit" variant="primary" disabled={saving || nothingToSave}>{t("profile.language.save")}</Button>
        </div>
      </FormSection>
    </div>
  );
}

export function ProfileDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const t = useT("panel");
  const close = () => { forgetFailedSwitch(); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title={t("profile.title")} centered closeButtonProps={{ "aria-label": t("action.close") }}>
      <LanguageForm onClose={close} />
    </Modal>
  );
}
