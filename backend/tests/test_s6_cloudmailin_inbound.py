"""CloudMailin JSON (Raw) inbound on POST /api/email-channel/inbound (routes/email_channel.py), real Mongo + ASGI route.
The payload shape follows CloudMailin's documented "JSON (Raw)" format; it is not a captured post.
Run alone: core.db binds one event loop, matching the repository CI convention.
"""
import os,sys,uuid,asyncio,base64,hmac,hashlib,json
from email.message import EmailMessage
from pathlib import Path
os.environ['DB_NAME']='huo_s6_cm_'+uuid.uuid4().hex[:8]
os.environ['EMAIL_PROVIDER']='log'
os.environ['EMAIL_CHANNEL_PROVIDER']='log'
os.environ['EMAIL_CHANNEL_WORKER']='true'
os.environ['EMAIL_CHANNEL_POSTMARK_TOKEN']='s6-inbound-test'
os.environ['EMAIL_CHANNEL_WEBHOOK_SECRET']='s6-hmac-test'
os.environ['EMAIL_CHANNEL_CLOUDMAILIN_TOKEN']='s6-cloudmailin-test'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
import pytest,httpx
from fastapi import FastAPI
from core import config
if config.DB_NAME!=os.environ['DB_NAME']:
    pytest.skip('Run this file in its own process',allow_module_level=True)
from core.db import db
from core.settings import update_settings
from services.email_channel import flow,intake
from routes.email_channel import router
app=FastAPI();app.include_router(router)
URL='/api/email-channel/inbound'
CM='Basic '+base64.b64encode(b'cloudmailin:s6-cloudmailin-test').decode()
PM='Basic '+base64.b64encode(b'postmark:s6-inbound-test').decode()


def run(coro): return loop.run_until_complete(coro)


def raw_mail(sender='asiakas@example.com',subject='Tarjouspyyntö Helsinki–Tukholma',extra_headers=()):
    m=EmailMessage();m['From']=sender;m['To']='abc123@cloudmailin.net';m['Subject']=subject
    m['Message-ID']=f'<{uuid.uuid4()}@mail.example.com>'
    for k,v in extra_headers:m[k]=v
    m.set_content('Hei, pyydän tarjousta 2 EUR-lavalle.')
    return m.as_string().replace('\n','\r\n')


def cm_payload(message,spf='pass',sender='asiakas@example.com'):
    return {'headers':{},'envelope':{'to':'abc123@cloudmailin.net','recipients':['abc123@cloudmailin.net'],'from':sender,
                                     'helo_domain':'mail.example.com','remote_ip':'192.0.2.10','tls':True,
                                     'spf':{'result':spf,'domain':sender.split('@')[-1]}},
            'message':message}


@pytest.fixture(autouse=True)
def clean():
    async def reset():
        for name in await db.list_collection_names():await db[name].delete_many({})
        await db.email_messages.create_index([('message_id',1),('direction',1)],unique=True)
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False,'email_channel_enabled':True})
    run(reset());yield


async def post(body,auth=None,signed=False):
    content=json.dumps(body).encode()
    headers={'content-type':'application/json'}
    if auth:headers['Authorization']=auth
    if signed:headers['X-Huolitsija-Signature']=hmac.new(b's6-hmac-test',content,hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        return await c.post(URL,content=content,headers=headers)


def test_cloudmailin_raw_json_with_basic_auth_is_stored_once():
    async def check():
        body=cm_payload(raw_mail())
        a=await post(body,CM);b=await post(body,CM)
        assert a.status_code==200 and a.json()['duplicate'] is False and b.json()['duplicate'] is True
        rows=await db.email_messages.find({},{'_id':0}).to_list(None)
        assert len(rows)==1 and rows[0]['from_email']=='asiakas@example.com' and rows[0]['source']=='webhook'
        assert rows[0]['subject']=='Tarjouspyyntö Helsinki–Tukholma'
    run(check())


def test_cloudmailin_without_the_right_basic_auth_is_refused():
    async def check():
        body=cm_payload(raw_mail())
        assert (await post(body)).status_code==401
        assert (await post(body,'Basic '+base64.b64encode(b'cloudmailin:wrong').decode())).status_code==401
        assert (await post(body,'Basic '+base64.b64encode(b'postmark:s6-cloudmailin-test').decode())).status_code==401
        assert (await post(body,'Basic !!!')).status_code==401
        assert await db.email_messages.count_documents({})==0
    run(check())


def test_cloudmailin_is_off_without_a_token(monkeypatch):
    monkeypatch.delenv('EMAIL_CHANNEL_CLOUDMAILIN_TOKEN')
    async def check():
        assert (await post(cm_payload(raw_mail()),CM)).status_code==401
        assert await db.email_messages.count_documents({})==0
    run(check())


def test_postmark_payload_still_works():
    async def check():
        payload={'From':'customer@example.com','To':'ops@huolitsija.fi','Subject':'Tarjouspyyntö','MessageID':'provider-id','TextBody':'Hei'}
        assert (await post(payload,CM)).status_code==200  # an authenticated provider; the body decides the format
        assert (await post({**payload,'MessageID':'provider-id-2'},PM)).status_code==200
        assert await db.email_messages.count_documents({})==2
    run(check())


def test_spf_result_is_recorded_only_from_the_authenticated_cloudmailin_envelope():
    async def check():
        await post(cm_payload(raw_mail(subject='A')),CM)
        await post(cm_payload(raw_mail(subject='B')),signed=True)  # HMAC caller sending this shape: no envelope trust
        await post(cm_payload(raw_mail(subject='C'),spf='pass\r\nX-Injected: 1'),CM)  # not an SPF result: nothing added
        await post(cm_payload(raw_mail(subject='D'),spf='softfail'),CM)
        got={r['subject']:r.get('auth_results') or '' async for r in db.email_messages.find({},{'_id':0})}
        assert got['A']=='cloudmailin.net; spf=pass smtp.mailfrom=asiakas@example.com'
        assert got['B']=='' and got['C']==''
        assert got['D']=='cloudmailin.net; spf=softfail smtp.mailfrom=asiakas@example.com'
    run(check())


def test_required_sender_authentication_accepts_cloudmailin_spf_pass(monkeypatch):
    monkeypatch.setenv('EMAIL_CHANNEL_INBOUND_REQUIRE_AUTH','true')
    async def parsed(*args,**kwargs):return {**intake.clean({}),'intent':'quote_request','confidence':1,'injection_suspected':True}
    monkeypatch.setattr(intake,'read_mail',parsed)  # stops right after the sender checks, before any quote
    async def check():
        await post(cm_payload(raw_mail(subject='pass')),CM)
        await post(cm_payload(raw_mail(subject='fail'),spf='fail'),CM)
        rows={r['subject']:r async for r in db.email_messages.find({},{'_id':0})}
        assert (await flow.process(rows['pass']))['decision']=='possible_prompt_injection'  # past the authentication gate
        assert (await flow.process(rows['fail']))['decision']=='sender_authentication_failed'
    run(check())
