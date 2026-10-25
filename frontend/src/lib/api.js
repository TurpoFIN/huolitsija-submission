// API client. Same-origin `/api` (Emergent ingress routes /api/* to the backend);
// REACT_APP_BACKEND_URL overrides it for split deployments.
import { useCallback, useEffect, useRef, useState } from "react";

import { localizePayload, currentLocale } from "./localizedData";
import { apiErrorMessage } from "./apiErrors";
import { useT } from "./i18n";

const BASE = `${(process.env.REACT_APP_BACKEND_URL || "").replace(/\/$/, "")}/api`;

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

function errorMessage(data, status) {
  if (!data) return `Request failed (${status})`;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  }
  return `Request failed (${status})`;
}

/** Headers every API request carries; also used by requests made outside api(), e.g. the 3D map's own fetch. */
export function sharedHeaders() {
  return localStorage.getItem("huo.includeTestTenants") === "true" ? { "X-Include-Test-Tenants": "true" } : {};
}

export async function api(path, { method = "GET", body, form, signal, headers, locale = currentLocale() } = {}) {
  const opts = { method, credentials: "include", signal, headers: { ...sharedHeaders(), ...(headers || {}) } };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`${BASE}${path}`, opts);
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, apiErrorMessage(0, null, locale), null);
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json().catch(() => null) : null;
  if (!res.ok) throw new ApiError(res.status, apiErrorMessage(res.status, errorMessage(data, res.status), locale), data);
  if (res.status === 202 && data && data.status === "processing" && data.request_id && data.poll) {
    return localizePayload(await followSlowRequest(data.poll, { signal, locale }), locale);
  }
  return localizePayload(data,locale);
}

/** A create that took over 25 s answers 202 and finishes on the server (core/bounded.py); wait for its response. */
export async function followSlowRequest(poll, { signal, locale = currentLocale(), intervalMs = 1500, maxMs = 300000 } = {}) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs));
    const row = await api(poll, { signal, locale });
    if (row && row.status === "done") return row.response;
    if (row && row.status === "failed") {
      const status = row.http_status || 500;
      throw new ApiError(status, apiErrorMessage(status, errorMessage(row.response, status), locale), row.response);
    }
  }
  throw new ApiError(504, apiErrorMessage(504, null, locale), null);
}

export const apiGet = (p, o) => api(p, { ...o, method: "GET" });
export const apiPost = (p, body, o) => api(p, { ...o, method: "POST", body });
export const apiPatch = (p, body, o) => api(p, { ...o, method: "PATCH", body });
export const apiPut = (p, body, o) => api(p, { ...o, method: "PUT", body });
export const apiDelete = (p, o) => api(p, { ...o, method: "DELETE" });
export const apiUpload = (p, formData, o) => api(p, { ...o, method: "POST", form: formData });
/** URL for a file the browser loads itself (link, <img>, <iframe>, window.open). Those requests cannot carry
 *  sharedHeaders(), so the test-tenant toggle travels as include_test=true (core/deps.py accepts either); without it
 *  ops saw {"detail":"Document not found"} for a test company's document preview. */
export const fileUrl = (p) =>
  sharedHeaders()["X-Include-Test-Tenants"] ? `${BASE}${p}${p.includes("?") ? "&" : "?"}include_test=true` : `${BASE}${p}`;

/**
 * useApi("/shipments?status=in_transit") -> { data, error, loading, refreshing, reload, setData, updatedAt }
 * Keeps the last good data on a failed refresh (show error + Retry next to it, never blank the view).
 */
export function useApi(path, { enabled = true, pollMs } = {}) {
  const { locale } = useT();
  const [state, setState] = useState({ data: null, error: null, loading: !!(enabled && path), refreshing: false, updatedAt: null });
  const ctrl = useRef(null);
  const load = useCallback(
    async (isRefresh = false) => {
      if (!enabled || !path) return;
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      setState((s) => ({ ...s, loading: !isRefresh && !s.data, refreshing: isRefresh || !!s.data, error: null }));
      try {
        const data = await apiGet(path, { signal: c.signal, locale });
        setState({ data, error: null, loading: false, refreshing: false, updatedAt: new Date() });
      } catch (e) {
        if (e.name === "AbortError") return;
        setState((s) => ({ ...s, error: e, loading: false, refreshing: false }));
      }
    },
    [path, enabled, locale]
  );
  useEffect(() => {
    load(false);
    return () => ctrl.current?.abort();
  }, [load]);
  useEffect(() => {
    if (!pollMs) return undefined;
    const t = setInterval(() => load(true), pollMs);
    return () => clearInterval(t);
  }, [pollMs, load]);
  const setData = useCallback((fn) => setState((s) => ({ ...s, data: typeof fn === "function" ? fn(s.data) : fn })), []);
  return { ...state, reload: () => load(true), setData };
}
