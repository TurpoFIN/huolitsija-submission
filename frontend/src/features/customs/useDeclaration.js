// Loads one declaration and wraps its edits/actions: PATCH with a save state, POST actions with busy + toast.
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { apiPatch, apiPost, useApi } from "@/lib/api";

export default function useDeclaration(id) {
  const [poll, setPoll] = useState(undefined);
  const q = useApi(id ? `/customs/${id}` : null, { pollMs: poll });
  const [save, setSave] = useState(null); // {key, state: saving|saved|error, error}
  const [busy, setBusy] = useState(null); // action name
  const timer = useRef(null);
  const { setData } = q;

  // Poll while the AI redraft runs in the background.
  const drafting = !!q.data?.drafting;
  useEffect(() => setPoll(drafting ? 4000 : undefined), [drafting]);
  useEffect(() => () => clearTimeout(timer.current), []);

  const patch = useCallback(async (body, key = "decl") => {
    setSave({ key, state: "saving" });
    try {
      const d = await apiPatch(`/customs/${id}`, body);
      setData(d);
      setSave({ key, state: "saved" });
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setSave((s) => (s?.key === key && s.state === "saved" ? null : s)), 4000);
      return d;
    } catch (e) {
      setSave({ key, state: "error", error: e });
      toast.error(e.message);
      throw e;
    }
  }, [id, setData]);

  const act = useCallback(async (name, path, body, okMsg) => {
    setBusy(name);
    try {
      const d = await apiPost(path, body);
      if (d && d.id === id) setData(d);
      if (okMsg) toast.success(okMsg);
      return d;
    } catch (e) {
      toast.error(e.message);
      throw e;
    } finally {
      setBusy(null);
    }
  }, [id, setData]);

  const saveState = (key) => (save?.key === key ? save.state : null);
  return { q, decl: q.data, patch, act, busy, saveState, setData };
}
