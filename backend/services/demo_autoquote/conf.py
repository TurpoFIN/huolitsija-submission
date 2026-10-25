"""Configuration of the autonomous quote demo. Read at call time, so a changed environment needs no code change.

DEMO_AUTOQUOTE_ENABLED          true = the demo routes and driver are available (default false: off on hosts that merge it)
DEMO_EMAIL_PROVIDER             provider for the demo's own mail (default EMAIL_PROVIDER), e.g. smtp2go
DEMO_EMAIL_FROM                 From: of every demo mail (default PARTNER_EMAIL_FROM, then EMAIL_FROM)
DEMO_INBOUND_ADDRESS            Reply-To of every demo mail: the mailbox Huolitsija's inbound poller reads (MAILTM_ADDRESS)
DEMO_REQUESTER_ADDRESS          requester sandbox inbox (mail.tm) and DEMO_REQUESTER_PASSWORD to read it
DEMO_PARTNER_SANDBOX_ADDRESS    when set, partner mail goes to this mailbox instead of the partner (rehearsals);
DEMO_PARTNER_SANDBOX_PASSWORD   the real address is still recorded and still counts against the recipient cap
DEMO_MAX_EXTERNAL_RECIPIENTS    external recipients per run, never above HARD_RECIPIENT_CAP
DEMO_TARGET_PARTNERS            partners contacted per run (default 6)
DEMO_REPLY_WINDOW_SECONDS       how long the run collects partner replies before it prices (default 240)
DEMO_SIMULATOR                  true = each contacted partner also gets a simulated reply (default true)
DEMO_SIM_MODEL                  model of the reply simulator (default LLM_MODEL)
DEMO_SIM_DELAY_MIN/MAX          seconds before a simulated reply arrives (default 25 / 110)
DEMO_REQUESTER_WAIT_SECONDS     wait for the requester's answer / the offer's arrival in the requester mailbox (default 1800)
DEMO_LIFECYCLE                  true = the offer hands the run to the world sim (demo.offer_sent); false = quote phase only
DEMO_REQUESTER_AUTO_ANSWER      true = the requester sandbox answers the agent's questions from the profile typed with
                                the request (default true)
"""
from core import config

HARD_RECIPIENT_CAP = 8


def enabled() -> bool:
    return config.env_bool("DEMO_AUTOQUOTE_ENABLED", False)


def provider() -> str:
    return config.env("DEMO_EMAIL_PROVIDER") or config.EMAIL_PROVIDER


def sender() -> str:
    return config.env("DEMO_EMAIL_FROM") or config.env("PARTNER_EMAIL_FROM") or config.EMAIL_FROM


def inbound_address() -> str:
    return (config.env("DEMO_INBOUND_ADDRESS") or config.env("MAILTM_ADDRESS")).lower()


def inbound_provider() -> str:
    """Customer sending may use Resend while replies retain the dedicated inbox."""
    explicit = config.env("DEMO_INBOUND_PROVIDER")
    if explicit:
        return explicit
    if provider() == "gmail":
        return "gmail"
    if provider() == "mailslurp" or (provider() == "resend" and config.env("DEMO_MAILSLURP_INBOX_ID")
                                    and config.env("HUOLITSIJA_DEMO_MAILSLURP_API_KEY")):
        return "mailslurp"
    return "mailtm"


def requester_address(run: dict | None = None) -> str:
    if run and run.get("real_requester"):
        return (run.get("requester") or {}).get("address", "").lower()
    return config.env("DEMO_REQUESTER_ADDRESS").lower()


def real_requester_enabled() -> bool:
    return enabled() and config.env_bool("DEMO_REAL_REQUESTER", False)


def real_requester_ready() -> bool:
    # Dedicated demo mailbox only: no production webhook is needed or changed.
    address = inbound_address()
    if inbound_provider() == "gmail":
        from services.email_channel import gmail_demo
        return gmail_demo.configured()
    if inbound_provider() == "mailslurp":
        return (real_requester_enabled() and config.env_bool("DEMO_WORLD_SANDBOX", False)
                and bool(address) and address != requester_address()
                and bool(config.env("DEMO_MAILSLURP_INBOX_ID"))
                and bool(config.env("HUOLITSIJA_DEMO_MAILSLURP_API_KEY")))
    return (real_requester_enabled() and config.env_bool("DEMO_WORLD_SANDBOX", False)
            and bool(address) and address == config.env("MAILTM_ADDRESS").lower()
            and address != requester_address() and bool(config.env("MAILTM_PASSWORD")))


def requester_password() -> str:
    return config.env("DEMO_REQUESTER_PASSWORD")


def partner_sandbox() -> tuple[str, str] | None:
    addr = config.env("DEMO_PARTNER_SANDBOX_ADDRESS").lower()
    return (addr, config.env("DEMO_PARTNER_SANDBOX_PASSWORD")) if addr else None


def max_recipients() -> int:
    try:
        n = int(config.env("DEMO_MAX_EXTERNAL_RECIPIENTS") or HARD_RECIPIENT_CAP)
    except ValueError:
        n = HARD_RECIPIENT_CAP
    return max(0, min(n, HARD_RECIPIENT_CAP))


def target_partners() -> int:
    try:
        n = int(config.env("DEMO_TARGET_PARTNERS") or 6)
    except ValueError:
        n = 6
    return max(1, min(n, max_recipients()))


def requester_wait_s() -> float:
    """How long the agent waits for the requester's answer, and the requester watcher for the offer's arrival."""
    try:
        return max(60.0, float(config.env("DEMO_REQUESTER_WAIT_SECONDS") or 1800))
    except ValueError:
        return 1800.0


def approval_wait_s() -> float:
    """How long an offer waits on its owner-queue card for a person before the run hands it over and stops."""
    try:
        return max(30.0, float(config.env("DEMO_APPROVAL_WAIT_SECONDS") or 1800))
    except ValueError:
        return 1800.0


def reply_window_s() -> float:
    try:
        return max(30.0, float(config.env("DEMO_REPLY_WINDOW_SECONDS") or 240))
    except ValueError:
        return 240.0


def simulator_on() -> bool:
    return config.env_bool("DEMO_SIMULATOR", True)


def sim_model() -> str:
    return config.env("DEMO_SIM_MODEL") or config.LLM_MODEL


def sim_delay() -> tuple[float, float]:
    try:
        lo, hi = float(config.env("DEMO_SIM_DELAY_MIN") or 25), float(config.env("DEMO_SIM_DELAY_MAX") or 110)
    except ValueError:
        lo, hi = 25.0, 110.0
    return max(1.0, lo), max(lo, hi)


def lifecycle_on() -> bool:
    return config.env_bool("DEMO_LIFECYCLE", True)


def requester_auto_answer() -> bool:
    return config.env_bool("DEMO_REQUESTER_AUTO_ANSWER", True)
