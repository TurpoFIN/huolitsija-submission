"""E-mail channel (Sähköpostikanava): a customer who only e-mails the company gets the whole flow by e-mail.
Importing the package registers the core.email outbound hook, the event handlers and the background loops."""
from . import hooks, mailtm, mailslurp, gmail_demo, worker  # noqa: F401
