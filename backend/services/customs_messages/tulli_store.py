"""Durable outgoing/incoming XML and the actual schema validation result."""
from core.db import db, register_index
from core.util import new_id, now_iso
from .tulli_common import validate

register_index('customs_messages', 'id', unique=True)
register_index('customs_messages', [('declaration_id', 1), ('at', 1)])

async def record(decl, xml, *, system, direction='out', channel, validation=None):
    result = validation if validation is not None else validate(xml, system)
    row = {'id': new_id(), 'declaration_id':decl['id'], 'company_id':decl.get('company_id'),
           'system':system, 'direction':direction, 'channel':channel, 'at':now_iso(),
           'xml':xml.decode() if isinstance(xml,bytes) else xml, 'validation':result,
           'type':result.get('message_type'), 'sandbox':channel=='sandbox'}
    await db.customs_messages.insert_one(dict(row))
    from core.events import emit
    await emit('customs.message_stored', {'declaration': decl, 'message': row})
    return row


async def prepare(decl, *, system, channel):
    from .tulli_common import TulliValidationError
    from . import tulli_import, tulli_export
    builder = tulli_import if system == 'import' else tulli_export
    try:
        xml = builder.build_message(decl, channel.upper())
    except TulliValidationError as exc:
        await record(decl, exc.xml, system=system, channel=channel, validation=exc.result)
        raise
    row = await record(decl, xml, system=system, channel=channel)
    return xml, row
