import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";
import { Button } from "./Button";
import { ConfirmDialog } from "./ConfirmDialog";
import { FormSection, SelectField, TextField } from "./Form";

const meta: Meta = { title: "Components/Forms" };
export default meta;

export const Form: StoryObj = {
  render: () => (
    <FormSection title="Add a user" onSubmit={() => undefined} error="That user name is taken.">
      <TextField label="User name" name="u" hint="Lowercase letters and digits." />
      <TextField label="Password" name="p" type="password" error="Too short." />
      <SelectField label="Role" name="r" options={[{ value: "user", label: "user" }, { value: "admin", label: "admin" }]} />
      <div className="ag-row"><Button type="submit" variant="primary">Add</Button></div>
    </FormSection>
  ),
};

function DialogDemo({ initiallyOpen = true }: { initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open</Button>
      <ConfirmDialog opened={open} title="Deactivate this user?" danger confirmLabel="Deactivate"
        onCancel={() => setOpen(false)} onConfirm={() => setOpen(false)}>
        <p>The user is signed out.</p>
      </ConfirmDialog>
    </>
  );
}
export const Dialog: StoryObj = { render: () => <DialogDemo /> };

/** Focus moves into the dialog, Tab stays inside it, and Escape returns focus to the opener. */
export const DialogFocus: StoryObj = {
  render: () => <DialogDemo initiallyOpen={false} />,
  play: async ({ canvasElement }) => {
    const opener = within(canvasElement).getByRole("button", { name: "Open" });
    await userEvent.click(opener);
    const dialog = await within(document.body).findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    for (let i = 0; i < 4; i++) {
      await userEvent.tab();
      await expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(document.activeElement).toBe(opener));
  },
};
