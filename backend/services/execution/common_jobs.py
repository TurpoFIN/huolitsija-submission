"""Jobs shared by the execution slice: the reply mailbox poll."""
from services.execution import mail
from services.execution.runtime import job, schedule


@job("execution.mail_poll", max_attempts=3, backoff=(60, 300, 900), timeout=120, agent=False,
     label_fi="Toteutuksen postilaatikko", label_en="Execution mailbox")
async def mail_poll(j: dict) -> dict:
    return {"ingested": await mail.poll_inbox()}


schedule("execution.mail_poll", job="execution.mail_poll", every_seconds=60,
         label_fi="Varaus- ja kuljetusvastausten haku", label_en="Booking desk and haulier replies")()
