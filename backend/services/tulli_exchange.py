"""Durable DME transmission identities and account-wide throttled reply retrieval."""
from datetime import datetime, timedelta, timezone
import hashlib

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core import config
from core.db import db
from core.util import now_iso
from services import tulli_transport as wire


def transport(mode):
    return wire.Transport(wire.Settings(
        mode=mode, declarant_id=config.TULLI_DECLARANT_ID,
        builder_id=config.env('TULLI_BUILDER_ID'), intermediary_id=config.env('TULLI_INTERMEDIARY_ID'),
        reference_prefix=config.env('TULLI_REFERENCE_PREFIX'),
        certificate=config.TULLI_CLIENT_CERT, private_key=config.TULLI_CLIENT_KEY,
        customer_approved=config.env('TULLI_CUSTOMER_APPROVED', 'false').lower() == 'true',
        endpoint=config.TULLI_ENDPOINT))


async def next_reference(client, system):
    cfg = client.settings
    cfg.require_ready()
    key = ':'.join(['tulli-reference', cfg.mode, cfg.declarant_id, system, cfg.reference_prefix])
    row = await db.raw.tulli_reference_counters.find_one_and_update({'_id': key}, {'$inc': {'value': 1}},
                                                   upsert=True, return_document=ReturnDocument.AFTER)
    if row['value'] > 999999999:
        raise wire.TransportError('Tulli interchange reference sequence exhausted')
    return cfg.reference_prefix + str(row['value']).zfill(9)


async def upload(client, system, decl, payload, reference):
    """Persist before sending; ambiguous receipt is never automatically sent again."""
    cfg = client.settings
    cfg.require_ready()
    canonical = wire.parse(payload)
    namespace = wire.E.QName(canonical).namespace
    identification = canonical.find(('{' + namespace + '}' if namespace else '') + 'Identification')
    if identification is not None:
        identification.text = ''  # Transport-generated ID must not defeat duplicate detection.
    identity = ':'.join([cfg.mode, cfg.declarant_id, system, decl['id'],
                         hashlib.sha256(wire.E.tostring(canonical, method='c14n')).hexdigest()])
    row = {'_id': identity, 'system': system, 'mode': cfg.mode, 'declaration_id': decl['id'],
           'company_id': decl.get('company_id'), 'declarant_id': cfg.declarant_id,
           'reference': reference, 'state': 'pending', 'created_at': now_iso(), 'payload': payload}
    try:
        await db.raw.tulli_transmissions.insert_one(row)
    except DuplicateKeyError:
        existing = await db.raw.tulli_transmissions.find_one({'_id': identity})
        if existing.get('state') == 'sent':
            return existing['result']
        raise wire.TransportError('Previous Tulli submission requires reconciliation before retry')
    try:
        response = await client.call('Upload', application=wire.APPLICATIONS[system],
                                     reference=reference, payload=payload)
        info = response.find('{' + wire.CT + '}ApplicationRequestMessageInformation')
        if info is None:
            raise wire.TransportError('Tulli acknowledgement has no storage identity')
        for key, expected in [('Application', wire.APPLICATIONS[system]),
                              ('DeclarantBusinessId', cfg.declarant_id), ('ControlReference', reference)]:
            if info.findtext('{' + wire.CT + '}' + key) != expected:
                raise wire.TransportError('Tulli upload acknowledgement identity mismatch: ' + key)
        result = {'channel': 'message_exchange', 'state': 'sent', 'mode': cfg.mode,
                  'reference': reference, 'storage_id': info.findtext('{' + wire.CT + '}MessageStorageId'),
                  'at': now_iso(), 'messages': []}
        await db.raw.tulli_transmissions.update_one({'_id': identity}, {'$set': {'state': 'sent', 'result': result}})
        return result
    except Exception:
        await db.raw.tulli_transmissions.update_one({'_id': identity}, {'$set': {'state': 'reconciliation_required'}})
        raise


async def refresh_replies(client):
    """One DownloadList per intermediary account per five minutes, across all systems."""
    cfg = client.settings
    cfg.require_ready()
    now = datetime.now(timezone.utc)
    from services.demo_world.boundary import authority_poll_seconds
    interval = authority_poll_seconds(305)
    key = cfg.mode + ':' + cfg.intermediary_id
    collection = db.raw.tulli_poll_cursors
    try:
        cursor = await collection.find_one_and_update(
            {'_id': key, '$or': [{'next_at': {'$lte': now}}, {'next_at': {'$exists': False}}]},
            {'$set': {'next_at': now + timedelta(seconds=interval)}}, upsert=True,
            return_document=ReturnDocument.BEFORE)
    except DuplicateKeyError:
        return
    # Overlap the last successful window so a crash cannot lose an authority reply.
    start = (cursor or {}).get('through', now - timedelta(days=7)) - timedelta(minutes=5)
    response = await client.call('DownloadList', start=start.isoformat(), end=now.isoformat())
    for info in response.findall('{' + wire.CT + '}MessageInformation'):
        values = {wire.E.QName(e).localname: e.text for e in info if len(e) == 0}
        if values.get('Application') not in wire.APPLICATIONS.values():
            continue
        ident = key + ':' + values['MessageStorageId']
        await db.raw.tulli_replies.update_one({'_id': ident}, {'$setOnInsert': {
            'account': key, 'info': values, 'applied': False}}, upsert=True)
    await collection.update_one({'_id': key}, {'$set': {'through': now}})


async def replies(client, system, decl):
    cfg = client.settings
    await refresh_replies(client)
    references = await db.raw.tulli_transmissions.distinct('reference', {
        'declaration_id': decl['id'], 'system': system, 'mode': cfg.mode, 'declarant_id': cfg.declarant_id})
    events = []
    async for row in db.raw.tulli_replies.find({
        'account': cfg.mode + ':' + cfg.intermediary_id, 'applied': False,
        'info.Application': wire.APPLICATIONS[system], 'info.DeclarantBusinessId': cfg.declarant_id,
        'info.ControlReference': {'$in': references}}).sort('info.MessageStoredTimestamp', 1):
        info = row['info']
        from services.demo_world.boundary import authority_reply_exists
        if not await authority_reply_exists(info['MessageStorageId'], cfg.intermediary_id):
            await db.raw.tulli_replies.delete_one({'_id': row['_id']})
            continue
        payload = row.get('payload')
        if payload is None:
            response = await client.call('Download', storage_id=info['MessageStorageId'])
            payload, reference = wire.application_response(response, application=wire.APPLICATIONS[system],
                declarant_id=cfg.declarant_id, storage_id=info['MessageStorageId'])
            if reference != info['ControlReference']:
                raise wire.TransportError('Tulli response control reference mismatch')
            payload = payload.decode('utf-8')
            await db.raw.tulli_replies.update_one({'_id': row['_id']}, {'$set': {'payload': payload,
                'application_response':wire.decode(response.findtext('{' + wire.CT + '}ApplicationResponseMessage')).decode('utf-8')}})
        root = wire.parse(payload)
        if root.tag == '{http://tulli.fi/schema/DmeDocumentInfoMessage}DmeDocumentInfoMessage':
            wire.validate(root, 'DmeDocumentInfoMessage')
            await db.raw.tulli_replies.update_one({'_id':row['_id']}, {'$set':{'applied':True,'kind':'document_metadata'}})
            continue
        if root.tag == '{http://tulli.fi/schema/DmeErrorMessage}DmeErrorMessage':
            wire.validate(root, 'DmeErrorMessage')
            types = '{http://tulli.fi/schema/DmeErrorMessageTypes}'
            header = root.find('{http://tulli.fi/schema/DmeErrorMessage}MessageIdentifier')
            if header.findtext(types+'ControlReference') != info['ControlReference'] or header.findtext(types+'MessageRecipient') != cfg.declarant_id:
                raise wire.TransportError('Tulli schema error identity mismatch')
            event = {'type':'rejected','status':'rejected','raw_type':'DmeErrorMessage',
                     'errors':[n.text for n in root.findall('.//'+types+'Details')]}
        elif system == 'transit':
            from services.customs_messages.ncts_fi import parse_response
            event = parse_response(payload)
        else:
            from services.customs_messages.tulli_responses import parse_response
            event = parse_response(payload, system)
        if event.get('lrn') and event['lrn'] != decl.get('lrn'):
            raise wire.TransportError('Tulli response LRN mismatch')
        if decl.get('mrn') and event.get('mrn') and decl['mrn'] != event['mrn']:
            raise wire.TransportError('Tulli response MRN mismatch')
        if not event.get('type'):
            raise wire.TransportError('Unsupported Tulli business response')
        if not row.get('message_id'):
            from services.customs_messages.tulli_store import record
            message = await record(decl, payload, system=system, direction='in', channel='message_exchange', validation=event.get('validation'))
            await db.raw.tulli_replies.update_one({'_id': row['_id']}, {'$set': {'message_id': message['id']}})
        events.append({**event, 'exchange_reply_id': row['_id'], 'storage_id': info['MessageStorageId'], 'xml': payload})
    return events


async def acknowledge(event):
    if event.get('exchange_reply_id'):
        await db.raw.tulli_replies.update_one({'_id': event['exchange_reply_id']},
                                             {'$set': {'applied': True, 'applied_at': now_iso()}})
