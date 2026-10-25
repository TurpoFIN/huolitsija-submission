// Test helper: render a component to static HTML with the real i18n provider (Finnish by default) and a router.
// Used with @jest-environment node (react-router v7 needs TextEncoder, which Jest 27's jsdom lacks).
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { I18nProvider } from "@/lib/i18n";

export function render(el, path = "/") {
  return renderToStaticMarkup(
    React.createElement(I18nProvider, null, React.createElement(MemoryRouter, { initialEntries: [path] }, el)),
  );
}

/** Visible text of rendered markup (tags stripped, entities decoded, whitespace collapsed). */
export function text(html) {
  return html.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
}
