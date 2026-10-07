import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no matchMedia; Mantine reads it for the color scheme and transitions.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent: () => false,
  });
}

// jsdom has no ResizeObserver; the Mantine Select (combobox) observes its dropdown.
window.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} };

// jsdom has no scrollIntoView; an open Select scrolls its selected option into view.
Element.prototype.scrollIntoView ??= function () {};

// jsdom has no FontFaceSet; the Mantine Textarea (autosize) listens for loaded fonts.
if (!document.fonts) {
  Object.defineProperty(document, "fonts", { value: { addEventListener() {}, removeEventListener() {} } });
}

afterEach(() => cleanup());
