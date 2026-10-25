"""ICS2 via a configured Domibus WS plugin (not an invented direct AS4 API).

SOAP 1.2 + ebMS UserMessage and base64 payload, EC WS plugin ICD 1.17,
sections 2.3.1/2.3.2. AP owns AS4 signing, encryption, receipts and retries.
PMode service/action/receiver identifiers must come from ICS2 onboarding.
"""
import base64
import ssl
from uuid import uuid4
import httpx
from lxml import etree as E
from core import config
from services.customs_messages.schema_codec import parse_xml
SOAP='http://www.w3.org/2003/05/soap-envelope'
EB='http://docs.oasis-open.org/ebxml-msg/ebms/v3.0/ns/core/200704/'
WS='http://org.ecodex.backend/1_1/'


def mode():return config.env('ICS2_MODE','disabled')


def status():
    required=['ICS2_EORI','ICS2_AS4_ENDPOINT','ICS2_AS4_CERT','ICS2_AS4_KEY','ICS2_PARTY_ID',
              'ICS2_RECEIVER_PARTY_ID','ICS2_SERVICE','ICS2_ACTION','ICS2_FINAL_RECIPIENT']
    missing=[k for k in required if not config.env(k)]
    m=mode()
    return {'mode':m,'channel':'sandbox' if m=='sandbox' else 'sti' if m=='sti' else 'domibus',
            'ready':m=='sandbox' or (m in {'conformance','production'} and not missing),
            'missing':missing if m in {'conformance','production'} else [],
            'description_fi':'ICS2: erillinen rekisteröinti ja riskianalyysivastaus vaaditaan',
            'description_en':'ICS2: separate registration and assessment responses are required'}


def envelope(operation, xml=None, message_id=None):
    env=E.Element(E.QName(SOAP,'Envelope'),nsmap={'soap':SOAP,'eb':EB,'ws':WS})
    header=E.SubElement(env,E.QName(SOAP,'Header'))
    body=E.SubElement(env,E.QName(SOAP,'Body'))
    req=E.SubElement(body,E.QName(WS,operation))
    def add(p,n,text=None,namespace=EB,**attrs):
        e=E.SubElement(p,E.QName(namespace,n) if namespace else n,**attrs)
        if text is not None:e.text=text
        return e
    if operation=='submitRequest':
        user=add(add(header,'Messaging'),'UserMessage')
        info=add(user,'MessageInfo');add(info,'MessageId',message_id)
        party=add(user,'PartyInfo')
        for side,key,role in [('From','ICS2_PARTY_ID','initiator'),('To','ICS2_RECEIVER_PARTY_ID','responder')]:
            p=add(party,side)
            add(p,'PartyId',config.env(key),type=config.env('ICS2_PARTY_ID_TYPE','urn:oasis:names:tc:ebcore:partyid-type:unregistered'))
            add(p,'Role',EB+role)
        coll=add(user,'CollaborationInfo')
        service=add(coll,'Service',config.env('ICS2_SERVICE'))
        if config.env('ICS2_SERVICE_TYPE'):service.set('type',config.env('ICS2_SERVICE_TYPE'))
        add(coll,'Action',config.env('ICS2_ACTION'))
        props=add(user,'MessageProperties')
        add(props,'Property',config.env('ICS2_EORI'),name='originalSender')
        add(props,'Property',config.env('ICS2_FINAL_RECIPIENT'),name='finalRecipient')
        part=add(add(user,'PayloadInfo'),'PartInfo',href='cid:message')
        add(add(part,'PartProperties'),'Property','text/xml',name='MimeType')
        p=add(req,'payload',namespace=None,payloadId='cid:message',contentType='text/xml')
        add(p,'value',base64.b64encode(xml.encode()).decode(),namespace=None)
    elif message_id:
        add(req,'messageID',message_id,namespace=None)
    return E.tostring(env,encoding='UTF-8',xml_declaration=True)


async def request(operation, xml=None, message_id=None):
    if mode() not in {'conformance','production'} or not status()['ready']:
        raise ValueError('ICS2 Domibus gateway is not configured')
    endpoint=config.env('ICS2_AS4_ENDPOINT')
    if not endpoint.startswith('https://'):raise ValueError('ICS2 AP endpoint must use HTTPS')
    ctx=ssl.create_default_context()
    ctx.load_cert_chain(config.env('ICS2_AS4_CERT'),config.env('ICS2_AS4_KEY'))
    auth=None
    if config.env('ICS2_AP_USERNAME'):
        auth=(config.env('ICS2_AP_USERNAME'),config.env('ICS2_AP_PASSWORD'))
    async with httpx.AsyncClient(verify=ctx,auth=auth,timeout=45,follow_redirects=False) as client:
        r=await client.post(endpoint,content=envelope(operation,xml,message_id),
                           headers={'Content-Type':'application/soap+xml; charset=UTF-8'})
        r.raise_for_status()
    root=parse_xml(r.content)
    if root.find('.//{'+SOAP+'}Fault') is not None:raise ValueError('Domibus returned a SOAP Fault')
    return root


async def submit(xml, message_id):
    root=await request('submitRequest',xml,message_id)
    ids=root.xpath('//*[local-name()="submitResponse"]/*[local-name()="messageID"]/text()')
    if not ids:raise ValueError('Domibus response omitted messageID; delivery uncertain')
    return {'channel':'domibus','transport_id':ids[0],'state':'submitted','sandbox':False}


async def receive():
    """Retrieve and durably stage AP messages before processing; retrieval consumes them."""
    from core.db import db
    from core.util import now_iso
    root=await request('listPendingMessagesRequest')
    ids=root.xpath('//*[local-name()="messageID"]/text()')
    for mid in ids[:50]:
        response=await request('retrieveMessageRequest',message_id=mid)
        values=response.xpath('//*[local-name()="payload"]/*[local-name()="value"]/text()')
        if not values:raise ValueError('AP returned no inline payload; configure inline/base64 WS plugin payloads')
        for n,value in enumerate(values):
            xml=base64.b64decode(value,validate=True).decode('utf-8')
            await db.raw.ics2_inbox.update_one({'_id':mid+':'+str(n)},
                {'$setOnInsert':{'xml':xml,'received_at':now_iso(),'processed':False}},upsert=True)
