/**
 * useClaimPending() — attaches a quote made on the public site to the signed-in customer's company.
 *
 * On mount, if the URL has ?claim=<token> or localStorage "huo.claim" is set (plain token, or JSON {token}):
 * POST /api/quotes/claim {token} -> {id, quote}; removes the stored token and navigates to /portal/quotes/<id>.
 * Any answer from the API (200, 404, 409, 410) removes the stored token; only network failures keep it for a later try.
 * Tokens tried in this tab session are remembered in sessionStorage "huo.claim.attempted": a leftover stored token that
 * was already tried is just removed (no second call, no second redirect).
 * Returns { claiming, error, dismiss }. Render it with <ClaimNotice/> (./ClaimNotice.jsx).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { apiPost } from "@/lib/api";

const KEY = "huo.claim";

function readStored() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    if (raw.trim().startsWith("{")) {
      const j = JSON.parse(raw);
      return j?.token || j?.claim_token || null;
    }
    return raw;
  } catch (e) {
    return null;
  }
}

function clearStored() {
  try {
    localStorage.removeItem(KEY);
  } catch (e) {}
}

const ATTEMPTED = "huo.claim.attempted"; // sessionStorage: tokens already tried in this tab session

function attempted() {
  try {
    return JSON.parse(sessionStorage.getItem(ATTEMPTED) || "[]");
  } catch (e) {
    return [];
  }
}

function markAttempted(token) {
  try {
    sessionStorage.setItem(ATTEMPTED, JSON.stringify([...attempted().filter((x) => x !== token), token].slice(-20)));
  } catch (e) {}
}

export default function useClaimPending() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [state, setState] = useState({ claiming: false, error: null });
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    const fromUrl = params.get("claim");
    const stored = readStored();
    const dropParam = () => {
      if (!fromUrl) return;
      const next = new URLSearchParams(params);
      next.delete("claim");
      setParams(next, { replace: true });
    };
    // A stored token this session already tried (e.g. claimed via ?claim= at sign-up) is only cleaned up: no call, no redirect.
    if (!fromUrl && stored && attempted().includes(stored)) {
      clearStored();
      return;
    }
    const token = fromUrl || stored;
    if (!token) return;
    ran.current = true;
    if (fromUrl && attempted().includes(fromUrl)) {
      if (stored === fromUrl) clearStored();
      dropParam();
      return;
    }
    markAttempted(token);
    setState({ claiming: true, error: null });
    apiPost("/quotes/claim", { token })
      .then((r) => {
        clearStored();
        const id = r?.id || r?.quote?.id;
        if (id) navigate(`/portal/quotes/${id}`, { replace: true });
        else {
          dropParam();
          setState({ claiming: false, error: null });
        }
      })
      .catch((e) => {
        if (e?.status !== 0) clearStored();
        dropParam();
        setState({ claiming: false, error: e });
      });
  }, [params, setParams, navigate]);

  const dismiss = useCallback(() => setState({ claiming: false, error: null }), []);
  return { ...state, dismiss };
}
