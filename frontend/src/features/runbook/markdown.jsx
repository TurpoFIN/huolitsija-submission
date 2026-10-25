// Small Markdown renderer for the owner runbook (headings, paragraphs, lists, tables, quotes, rules, inline code/bold/
// italic/links). It builds React elements, never HTML strings. Links to another runbook document open it in the page;
// web links open in a new tab; repository paths stay as plain code.
import React from "react";

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/** Markdown text -> blocks: {type: h|p|ul|ol|table|quote|hr|code, ...}. */
export function parseBlocks(md) {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (line.startsWith("```")) {
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) { body.push(lines[i]); i += 1; }
      out.push({ type: "code", text: body.join("\n") });
      i += 1;
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) { out.push({ type: "h", level: h[1].length, text: h[2].trim() }); i += 1; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push({ type: "hr" }); i += 1; continue; }
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
      out.push({ type: "table", head, rows });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) { body.push(lines[i].replace(/^\s*>\s?/, "")); i += 1; }
      out.push({ type: "quote", text: body.join(" ") });
      continue;
    }
    const ul = /^\s*[-*+]\s+/;
    const ol = /^\s*\d+[.)]\s+/;
    if (ul.test(line) || ol.test(line)) {
      const re = ul.test(line) ? ul : ol;
      const items = [];
      while (i < lines.length && lines[i].trim()) {
        if (re.test(lines[i])) items.push(lines[i].replace(re, ""));
        else if (items.length && /^\s+/.test(lines[i])) items[items.length - 1] += ` ${lines[i].trim()}`;
        else break;
        i += 1;
      }
      out.push({ type: re === ul ? "ul" : "ol", items });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*>|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i])
      && !(TABLE_ROW.test(lines[i]) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]))) {
      para.push(lines[i].trim());
      i += 1;
    }
    out.push({ type: "p", text: para.join(" ") });
  }
  return out;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)\s]+\))|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)/g;

/** Inline Markdown -> tokens: {t: text|code|b|i|a, v, href}. */
export function parseInline(text) {
  const out = [];
  let last = 0;
  for (const m of (text || "").matchAll(INLINE)) {
    if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
    const s = m[0];
    if (m[1]) out.push({ t: "code", v: s.slice(1, -1) });
    else if (m[2]) out.push({ t: "b", v: s.slice(2, -2) });
    else if (m[3]) {
      const [, label, href] = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(s);
      out.push({ t: "a", v: label, href });
    } else out.push({ t: "i", v: s.slice(1, -1) });
    last = m.index + s.length;
  }
  if (last < (text || "").length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

// Bold text may hold links or code ("**see [X](X.md)**"): its inner tokens are rendered by the same function.
function renderTokens(tokens, ctx, prefix = "") {
  return tokens.map((tok, n) => {
    const k = `${prefix}${n}`;
    if (tok.t === "code") return <code key={k} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{tok.v}</code>;
    if (tok.t === "b") return <strong key={k} className="font-semibold">{renderTokens(parseInline(tok.v), ctx, `${k}.`)}</strong>;
    if (tok.t === "i") return <em key={k}>{tok.v}</em>;
    if (tok.t === "a") {
      const file = tok.href.split("#")[0].split("/").pop();
      if (ctx.files && ctx.files[file]) {
        return <button key={k} type="button" className="text-primary underline underline-offset-2" onClick={() => ctx.onDoc(ctx.files[file])}>{tok.v}</button>;
      }
      if (/^https?:\/\//.test(tok.href)) {
        return <a key={k} href={tok.href} target="_blank" rel="noreferrer noopener" className="text-primary underline underline-offset-2">{tok.v}</a>;
      }
      return <span key={k}>{tok.v} <code className="font-mono text-[0.85em] text-ink-secondary">{tok.href}</code></span>;
    }
    return <React.Fragment key={k}>{tok.v}</React.Fragment>;
  });
}

function Inline({ text, onDoc, files }) {
  return renderTokens(parseInline(text), { onDoc, files });
}

const H = { 1: "text-xl font-semibold mt-2 mb-3", 2: "text-lg font-semibold mt-8 mb-2", 3: "text-base font-semibold mt-6 mb-2" };

/** files: {"OWNER-ACTIVATION.md": "activation", ...}; onDoc(id) opens that document. */
export function Markdown({ text, files, onDoc }) {
  return (
    <div className="max-w-none text-sm leading-6 text-foreground">
      {parseBlocks(text).map((b, k) => {
        const inl = (s) => <Inline text={s} files={files} onDoc={onDoc} />;
        if (b.type === "h") {
          const Tag = `h${Math.min(b.level + 1, 6)}`;
          return <Tag key={k} className={H[b.level] || "text-sm font-semibold mt-4 mb-1"}>{inl(b.text)}</Tag>;
        }
        if (b.type === "p") return <p key={k} className="my-3">{inl(b.text)}</p>;
        if (b.type === "hr") return <hr key={k} className="my-6 border-border" />;
        if (b.type === "code") return <pre key={k} className="my-3 overflow-x-auto rounded bg-muted p-3 font-mono text-xs">{b.text}</pre>;
        if (b.type === "quote") return <blockquote key={k} className="my-3 border-l-2 border-border pl-3 text-ink-secondary">{inl(b.text)}</blockquote>;
        if (b.type === "ul" || b.type === "ol") {
          const Tag = b.type;
          return (
            <Tag key={k} className={`my-3 space-y-1 pl-5 ${b.type === "ul" ? "list-disc" : "list-decimal"}`}>
              {b.items.map((it, j) => <li key={j}>{inl(it)}</li>)}
            </Tag>
          );
        }
        if (b.type === "table") {
          return (
            <div key={k} className="my-4 overflow-x-auto rounded border border-border">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="bg-muted/60">
                  <tr>{b.head.map((c, j) => <th key={j} className="px-2 py-2 align-top font-medium">{inl(c)}</th>)}</tr>
                </thead>
                <tbody>
                  {b.rows.map((r, j) => (
                    <tr key={j} className="border-t border-border">
                      {r.map((c, n) => <td key={n} className="px-2 py-2 align-top">{inl(c)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        return null;
      })}
    </div>
  );
}
