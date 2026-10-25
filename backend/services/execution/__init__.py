"""Execution layer (s9): what happens after the carrier booking. Importing the package registers every job, schedule,
event handler and e-mail reply handler of the slice.

    si_vgm        shipping instructions + VGM (DCSA EBL 3.0 / VGM 1.0 API, or e-mail to the booking desk)
    cutoffs       documentation / VGM / cargo cut-offs per booking, alerts before and at each cut-off
    slots         pickup and delivery slots with hauliers (partner portal + e-mail with signed links)
    consolidation LCL/groupage masters, container load plan, deconsolidation at destination
    crossdock     cross-dock steps at a hub (arrive, unload, sort, reload, depart)
    warehouse     inbound/outbound warehouse orders, receipt/dispatch milestones; WMS_PROVIDER none|email|odoo
"""
from services.execution import runtime, mail  # noqa: F401
from services.execution import common_jobs  # noqa: F401
from services.execution import cutoffs, si_vgm, slots, consolidation, crossdock, warehouse  # noqa: F401
