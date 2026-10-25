import fs from "fs";
import path from "path";
import dictionaries from "@/i18n";
import { OPS_NAV } from "@/nav";
import { parseBlocks, parseInline } from "@/features/runbook/markdown";

const DOCS = path.resolve(__dirname, "../../../../backend/data/runbook");

test("ops nav has the owner runbook in the admin group, labelled in both languages", () => {
  const admin = OPS_NAV.find((g) => g.label === "nav.group.admin").items;
  const item = admin.find((i) => i.to === "/ops/runbook");
  expect(item).toBeTruthy();
  expect([dictionaries.en[item.label], dictionaries.fi[item.label]]).toEqual(["Runbook", "Käsikirja"]);
  ["runbook", "activation", "legal-points", "final-run", "processors", "config"].forEach((id) => {
    expect(dictionaries.fi[`runbook.doc.${id}`]).toBeTruthy();
    expect(dictionaries.en[`runbook.doc.${id}`]).toBeTruthy();
  });
});

test("the shipped runbook parses into headings, paragraphs and complete tables", () => {
  const md = fs.readFileSync(path.join(DOCS, "RUNBOOK.md"), "utf8");
  const blocks = parseBlocks(md);
  expect(blocks[0]).toEqual({ type: "h", level: 1, text: "Huolitsija operating and go-live runbook" });
  const tables = blocks.filter((b) => b.type === "table");
  expect(tables.length).toBeGreaterThan(0);
  tables.forEach((tb) => tb.rows.forEach((r) => expect(r.length).toBe(tb.head.length)));
  // no table row leaks into a paragraph
  blocks.filter((b) => b.type === "p").forEach((b) => expect(b.text.startsWith("|")).toBe(false));
});

test("markdown structure: lists, rules, quotes and inline marks", () => {
  const blocks = parseBlocks("# T\n\nOne\ntwo\n\n- a\n- b\n  more\n\n1. x\n2. y\n\n---\n\n> note\n\n| A | B |\n|---|:-:|\n| 1 | 2 |");
  expect(blocks.map((b) => b.type)).toEqual(["h", "p", "ul", "ol", "hr", "quote", "table"]);
  expect(blocks[1].text).toBe("One two");
  expect(blocks[2].items).toEqual(["a", "b more"]);
  expect(blocks[6]).toEqual({ type: "table", head: ["A", "B"], rows: [["1", "2"]] });
  expect(parseInline("See [Owner](OWNER-ACTIVATION.md), **bold** `x` and *it*.")).toEqual([
    { t: "text", v: "See " }, { t: "a", v: "Owner", href: "OWNER-ACTIVATION.md" }, { t: "text", v: ", " },
    { t: "b", v: "bold" }, { t: "text", v: " " }, { t: "code", v: "x" }, { t: "text", v: " and " }, { t: "i", v: "it" }, { t: "text", v: "." },
  ]);
});

test("a link inside bold text renders as a link, not as raw markdown", () => {
  const React = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { Markdown } = require("@/features/runbook/markdown");
  const html = renderToStaticMarkup(React.createElement(Markdown, {
    text: "**Duties in [LEGAL-HUMAN-POINTS.md](LEGAL-HUMAN-POINTS.md) and [web](https://example.org).**",
    files: { "LEGAL-HUMAN-POINTS.md": "legal-points" }, onDoc: () => {},
  }));
  expect(html).not.toContain("](");
  expect(html).toContain("<strong");
  expect(html).toContain(">LEGAL-HUMAN-POINTS.md</button>");
  expect(html).toContain('href="https://example.org"');
});
