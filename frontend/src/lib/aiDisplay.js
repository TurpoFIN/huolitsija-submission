export function aiErrorInfo(run,t) {
  const error=run.error||run.note||'';
  if(/429|rate.?limit|concurrent_request_limit/i.test(error))return {reason:t('review.ai.ratelimit'),next:t('review.ai.ratelimit_next')};
  if(/not configured|No AI provider|API.?key|authentication|401/i.test(error))return {reason:t('review.ai.unavailable'),next:t('review.ai.unavailable_next')};
  if(/timeout|timed out|deadline/i.test(error))return {reason:t('review.ai.timeout'),next:t('review.ai.retry')};
  if(/JSON|parse|empty|no summary/i.test(error))return {reason:t('review.ai.invalid'),next:t('review.ai.retry')};
  return {reason:t(error?'review.ai.other':'review.ai.no_error'),next:t('review.ai.retry')};
}
export function agentLabel(agent,t) {
  const key=`review.ai.agent.${agent}`;return t(key)===key?t('review.ai.unknown_agent'):t(key);
}
export function recordLabel(ref,t) {
  const key=`review.record.${ref?.type}`;return `${t(key)===key?t('review.record.unknown'):t(key)} ${String(ref?.id||'').slice(0,8)}`;
}

// Keep retired provider identifiers out of every rendered history surface.
export function aiRunGroups(items = [], provider) {
  const current = [], earlier = [];
  for (const run of items) {
    if (provider === 'openai' && run.provider === provider) current.push(run);
    else earlier.push({ ...run, provider: 'legacy', model: null, legacy: true,
      prompt_preview: null, output_preview: null });
  }
  return { current, earlier };
}
export function aiRunLabel(run) {
  return run.legacy || run.provider !== 'openai' ? 'legacy' : (run.model || run.provider);
}
