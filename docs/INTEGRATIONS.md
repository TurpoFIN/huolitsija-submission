# Integration setup

`backend/core/config.py` reads `backend/.env`; additional domain settings are read from the environment at call time. Start from `backend/.env.example`. Never commit a filled environment file.

| Capability | Required configuration | Local baseline |
| --- | --- | --- |
| Database | `MONGO_URL`, `DB_NAME` | Local Compose MongoDB, isolated port 27027 |
| Login | Per-role demo passwords, or application-created users | Demo users created only for configured passwords |
| AI | `OPENAI_API_KEY`, `LLM_MODEL`; optional `OPENAI_BASE_URL` | No key supplied; AI-dependent work needs configuration |
| Email | `EMAIL_PROVIDER`, verified sender and selected provider credentials | `log`: database outbox only |
| Payments | Stripe credentials and webhook signing secret | Unconfigured |
| Customs filing | Tulli mode, endpoints, registered declarant and certificates | Disabled |
| Tracking / maps | Optional carrier keys and service URL overrides | Public open-data endpoints may be contacted |
| SMS | Twilio account, token and sending number | Log mode |

The optional email demo supports dedicated Gmail, Resend, SMTP2GO and mailbox transports. The current Gmail adapter is bound to the original demo alias in `services/email_channel/gmail_demo.py`; it is not a generic Gmail onboarding flow. No OAuth grant is included. Do not set demo flags merely to browse the portal: real mail additionally requires the dedicated credentials, authorized sender, allowed recipients, inbound routing and sandbox boundary. See the configuration module `services/demo_autoquote/conf.py` and the existing provider-boundary tests before configuring it.

For a split production deployment, serve the frontend build and proxy `/api` to FastAPI. Configure an explicit public origin, HTTPS cookies, allowed origins, strong authentication and the integrations you intend to operate. The included Compose service is for local development. The retained operational/legal runbooks are historical source material and require review against the actual installation.
