// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { AutocompleteLoading as AutocompleteLoadingPattern } from "./AutocompleteLoading/AutocompleteLoading";
import { CheckboxCard as CheckboxCardPattern } from "./CheckboxCard/CheckboxCard";
import { ContainedInputs as ContainedInputsPattern } from "./ContainedInputs/ContainedInputs";
import { CurrencyInput as CurrencyInputPattern } from "./CurrencyInput/CurrencyInput";
import { CustomSwitch as CustomSwitchPattern } from "./CustomSwitch/CustomSwitch";
import { FloatingLabelInput as FloatingLabelInputPattern } from "./FloatingLabelInput/FloatingLabelInput";
import { ForgotPasswordInput as ForgotPasswordInputPattern } from "./ForgotPasswordInput/ForgotPasswordInput";
import { GradientSegmentedControl as GradientSegmentedControlPattern } from "./GradientSegmentedControl/GradientSegmentedControl";
import { ImageCheckboxes as ImageCheckboxesPattern } from "./ImageCheckboxes/ImageCheckboxes";
import { InputTooltip as InputTooltipPattern } from "./InputTooltip/InputTooltip";
import { InputValidation as InputValidationPattern } from "./InputValidation/InputValidation";
import { InputWithButton as InputWithButtonPattern } from "./InputWithButton/InputWithButton";
import { LanguagePicker as LanguagePickerPattern } from "./LanguagePicker/LanguagePicker";
import { PasswordStrength as PasswordStrengthPattern } from "./PasswordStrength/PasswordStrength";

const meta: Meta = { title: "Lookbook/Inputs", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const AutocompleteLoading: Story = { name: "Autocomplete async data", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><AutocompleteLoadingPattern /></div> };
export const CheckboxCard: Story = { name: "Card with checkbox", render: () => <div style={{"maxWidth": 400, "margin": "0 auto"}}><CheckboxCardPattern /></div> };
export const ContainedInputs: Story = { name: "Inputs with label inside input", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><ContainedInputsPattern /></div> };
export const CurrencyInput: Story = { name: "Number input with currency select", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><CurrencyInputPattern /></div> };
export const CustomSwitch: Story = { name: "Custom switch", render: () => <div style={{"margin": "0 auto"}}><CustomSwitchPattern /></div> };
export const FloatingLabelInput: Story = { name: "Input with floating label", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><FloatingLabelInputPattern /></div> };
export const ForgotPasswordInput: Story = { name: "Forgot password on input label", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><ForgotPasswordInputPattern /></div> };
export const GradientSegmentedControl: Story = { name: "Gradient segmented control", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><GradientSegmentedControlPattern /></div> };
export const ImageCheckboxes: Story = { name: "Checkbox with image", render: () => <div style={{"maxWidth": 1080, "margin": "0 auto"}}><ImageCheckboxesPattern /></div> };
export const InputTooltip: Story = { name: "Inputs with tooltip", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><InputTooltipPattern /></div> };
export const InputValidation: Story = { name: "Input with custom validation styles", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><InputValidationPattern /></div> };
export const InputWithButton: Story = { name: "Input with contained button", render: () => <div style={{"maxWidth": 520, "margin": "0 auto"}}><InputWithButtonPattern /></div> };
export const LanguagePicker: Story = { name: "Language picker", render: () => <div style={{"maxWidth": 220, "margin": "0 auto"}}><LanguagePickerPattern /></div> };
export const PasswordStrength: Story = { name: "Password with strength meter", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><PasswordStrengthPattern /></div> };
