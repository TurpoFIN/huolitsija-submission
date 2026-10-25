"""Autopilot used to be one loop here that approved waiting cards (AI replies, quotes, customs filings) and issued
invoices. That work now runs as durable jobs, one agent per kind of work, behind one decision gate with guardrails
and an audit row per decision (services.agents, core.agent_runtime.decide): quote_agent, comms_agent,
customs_agent, invoice_agent. Importing this module registers them (kept for older imports, e.g. routes.seed)."""
import services.agents  # noqa: F401


async def run_autopilot():
    """Kept for older callers: the agents run as jobs now, so there is nothing to do here."""
    return None
