import React, { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";
import { apiGet } from "@/lib/api";

export function OperationalVisibility(){
  const {t}=useT();const [show,setShow]=useState(()=>localStorage.getItem('huo.includeTestTenants')==='true');const [active,setActive]=useState(false);
  useEffect(()=>{const load=()=>apiGet('/autonomy/status').then(d=>setActive(d.autopilot_enabled)).catch(()=>{});load();window.addEventListener('huo.autonomy',load);const timer=setInterval(load,30000);return()=>{clearInterval(timer);window.removeEventListener('huo.autonomy',load);};},[]);
  return <div className="flex flex-wrap items-center gap-2 text-xs">{active&&<span className="status-in-progress rounded-full px-2 py-1" role="status">{t('domain.active')}</span>}<label className="flex items-center gap-1.5"><input type="checkbox" checked={show} onChange={e=>{localStorage.setItem('huo.includeTestTenants',String(e.target.checked));setShow(e.target.checked);window.location.reload();}}/>{t('domain.show_tests')}</label></div>;
}

export function EnvironmentBanner(){
  const {t}=useT();const [show,setShow]=useState(false);useEffect(()=>{apiGet('/environment').then(d=>setShow(d.example_data)).catch(()=>{});},[]);
  return show?<div className="border-b border-[color:var(--border-subtle)] bg-secondary px-4 py-2 text-xs text-ink-secondary" role="status">{t('domain.env')}</div>:null;
}
