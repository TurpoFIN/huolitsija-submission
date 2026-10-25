import fs from "fs";
import path from "path";
import { fileUrl, sharedHeaders } from "./api";

// s4 D2: the ops 3D map fetched /api/map/shipments without the test-tenant header the rest of the app sends.
test("sharedHeaders follows the test-tenant toggle", () => {
  localStorage.removeItem("huo.includeTestTenants");
  expect(sharedHeaders()).toEqual({});
  localStorage.setItem("huo.includeTestTenants", "true");
  expect(sharedHeaders()).toEqual({ "X-Include-Test-Tenants": "true" });
  localStorage.setItem("huo.includeTestTenants", "false");
  expect(sharedHeaders()).toEqual({});
});

test("the map's own requests carry the shared headers", () => {
  const src = fs.readFileSync(path.resolve(__dirname, "../features/map/ShipmentMap.jsx"), "utf8");
  expect(src).toMatch(/getHeaders=\{sharedHeaders\}/);
});

// r3: links, <img> and <iframe> to /api cannot send headers, so the toggle goes in the query string.
test("fileUrl carries the test-tenant toggle as include_test", () => {
  localStorage.removeItem("huo.includeTestTenants");
  expect(fileUrl("/documents/d1/file")).toBe("/api/documents/d1/file");
  localStorage.setItem("huo.includeTestTenants", "true");
  expect(fileUrl("/documents/d1/file")).toBe("/api/documents/d1/file?include_test=true");
  expect(fileUrl("/documents/d1/preview?page=1&width=1200")).toBe("/api/documents/d1/preview?page=1&width=1200&include_test=true");
  localStorage.setItem("huo.includeTestTenants", "false");
  expect(fileUrl("/invoices/i1/pdf")).toBe("/api/invoices/i1/pdf");
});

test("files under /api are linked through fileUrl only", () => {
  const offenders = [];
  (function walk(dir) {
    for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, f.name);
      if (f.isDirectory()) walk(p);
      else if (/\.jsx?$/.test(f.name) && !/\.test\./.test(f.name) && !p.endsWith(path.join("lib", "api.js"))) {
        const src = fs.readFileSync(p, "utf8");
        if (/(href|src)=\{?[`"]\/api\//.test(src) || /window\.open\([`"]\/api\//.test(src)) offenders.push(p);
      }
    }
  })(path.resolve(__dirname, ".."));
  expect(offenders).toEqual([]);
});

// s3: a create over 25 s answers 202 and finishes on the server; the client returns the final response.
function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, headers: { get: () => "application/json" }, json: async () => body };
}

test("a 202 processing create is followed to its final response", async () => {
  const { apiPost } = require("./api");
  const calls = [];
  const replies = [
    jsonResponse(202, { status: "processing", request_id: "r1", poll: "/requests/r1" }),
    jsonResponse(200, { status: "processing" }),
    jsonResponse(200, { status: "done", http_status: 200, response: { shipment: { id: "s1", reference: "HUO-1" } } }),
  ];
  global.fetch = jest.fn(async (url) => { calls.push(url); return replies.shift(); });
  jest.useFakeTimers();
  const pending = apiPost("/shipments", { company_id: "c" });
  for (let i = 0; i < 5; i++) { await Promise.resolve(); jest.advanceTimersByTime(1500); await Promise.resolve(); }
  jest.useRealTimers();
  await expect(pending).resolves.toEqual({ shipment: { id: "s1", reference: "HUO-1" } });
  expect(calls.filter((u) => u.endsWith("/requests/r1")).length).toBe(2);
});

test("a 202 create that fails on the server raises its real status", async () => {
  const { apiPost, ApiError } = require("./api");
  const replies = [
    jsonResponse(202, { status: "processing", request_id: "r2", poll: "/requests/r2" }),
    jsonResponse(200, { status: "failed", http_status: 409, response: { detail: "Booking needs an approved company check (KYC)." } }),
  ];
  global.fetch = jest.fn(async () => replies.shift());
  const p = apiPost("/shipments", {}).catch((e) => e);
  const err = await new Promise((resolve) => setTimeout(() => resolve(p), 1700)).then((x) => x);
  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(409);
});
