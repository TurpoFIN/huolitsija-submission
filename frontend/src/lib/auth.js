import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { apiGet, apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";

const AuthCtx = createContext(null);

export function AuthProvider({ children }) {
  const [state, setState] = useState({ user: null, company: null, loading: true });
  const { setLocale } = useT();

  const refresh = useCallback(async () => {
    try {
      const r = await apiGet("/auth/me");
      setState({ user: r.user, company: r.company || null, loading: false });
      if (r.user?.locale) setLocale(r.user.locale);
      return r;
    } catch (e) {
      setState({ user: null, company: null, loading: false });
      return null;
    }
  }, [setLocale]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = useCallback(async (email, password) => {
    await apiPost("/auth/login", { email, password });
    return refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    await apiPost("/auth/logout", {}).catch(() => {});
    setState({ user: null, company: null, loading: false });
  }, []);

  const value = useMemo(() => ({ ...state, refresh, login, logout,
    can: (perm) => !!state.user?.permissions?.includes(perm) }), [state, refresh, login, logout]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth() {
  return useContext(AuthCtx);
}

export function homeFor(user) {
  if (!user) return "/login";
  if (user.org_type === "internal") return "/ops";
  if (user.org_type === "partner") return "/partner";
  return "/portal";
}

/** Route guard. orgTypes: ["customer"] | ["internal"] | ["partner"]. */
export function RequireAuth({ orgTypes, children }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="p-6 text-sm text-ink-tertiary">…</div>;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  if (orgTypes && !orgTypes.includes(user.org_type)) return <Navigate to={homeFor(user)} replace />;
  return children;
}
