"""Transit consumes the Tulli lane's shared message-exchange boundary.

Uses official v1.21 transport, gated on customer registration and certificate.
"""
from core import config
from services.tulli import MessageExchangeGateway


def gateway():
    return MessageExchangeGateway(config.env('TULLI_TRANSIT_MODE',config.env('TULLI_MODE','disabled')),'transit')


def status():return gateway().status()


def require_ready():
    s=status()
    if not s.get('ready'):
        raise ValueError('Tulli transit transport unavailable: '+', '.join(s.get('missing',[])))


async def submit(xml,message_type,decl,message_id):
    require_ready()
    adapter=gateway()
    # Only a transport that explicitly exposes raw validated business XML may
    # accept national transit messages. Import XML must never be substituted.
    if not hasattr(adapter,'submit_xml'):
        raise ValueError('Tulli shared gateway has no validated XML transport adapter')
    result=await adapter.submit_xml(xml,message_type=message_type,message_id=message_id,decl=decl)
    if result.get('state') not in {'sent','submitted'}:raise ValueError('Tulli did not acknowledge transport submission')
    return result


async def poll():
    require_ready()
    from core.db import db
    from core.util import NO_ID
    from services.customs_transit import record_response
    from services.tulli_exchange import acknowledge
    count=0
    adapter=gateway()
    async for decl in db.raw.transit_declarations.find({'submission.channel':'message_exchange'},NO_ID):
        for event in await adapter.poll(decl):
            if event.get('raw_type') == 'DmeErrorMessage':
                from services import customs_flow
                await customs_flow.transition('transit', decl, 'rejected', extra={'last_response':event})
            else:
                await record_response(decl['id'],event['xml'],'message_exchange')
            await acknowledge(event)
            count+=1
    return count
