"""Draft/export through the existing exporter from real source documents.

No sandbox knowledge: classification, office lookup, screening, licence controls
and delegated filing all run through their ordinary services and guardrails.
"""
from copy import deepcopy
import hashlib

from core import config
from core.agent_runtime import agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import enqueue, job, JobDeferred
from core.settings import get_settings
from core.util import NO_ID, now_iso
from services.agents.base import entity_lock, customer_message, sweeper, mode_key, agents_epoch


def customer_arranged(shipment):
    return shipment.get("customs_handling") in ("broker", "customer_broker") or \
        shipment.get("customs_filing_status") == "customer_arranged"


async def schedule(shipment, cause):
    if customer_arranged(shipment):
        return
    if shipment.get('direction') != 'export' or not shipment.get('customs_clearance') or shipment.get('demo'):
        return
    from services.documents import EU_COUNTRIES
    if (shipment.get('destination') or {}).get('country') in EU_COUNTRIES:
        return
    await enqueue('customs.review_export', {'shipment_id': shipment['id']},
                  key='review-export:' + shipment['id'] + ':' + cause, company_id=shipment.get('company_id'))


@on('document.extracted')
async def document_extracted(p):
    d = p.get('document') or {}
    if d.get('type') not in ('commercial_invoice', 'packing_list') or not d.get('shipment_id'):
        return
    s = await db.raw.shipments.find_one({'id': d['shipment_id']}, NO_ID)
    if s:
        await schedule(s, d['id'] + ':' + str((d.get('extraction') or {}).get('completed_at')))


@on('carrier.booked')
async def carrier_booked(p):
    await schedule(p.get('shipment') or {}, 'carrier:' + str((p.get('booking') or {}).get('id')))


@on('shipment.customs_data_updated')
async def instructions(p):
    s = p['shipment']
    await schedule(s, 'instructions:' + now_iso())
    if s.get('direction') == 'import':
        from services.customs import draft_declaration_for_shipment
        await draft_declaration_for_shipment(s['id'], agent_bot('customs_agent'))


@job('customs.review_export', agent=True, timeout=300, max_attempts=4, backoff=(15, 60, 180))
async def review(j):
    from services import customs, customs_export as exporter, customs_offices, hs
    from services.tradedocs import data
    from services.agents.customs_agent import item_confidence
    s = await db.raw.shipments.find_one({'id': j['payload']['shipment_id']}, NO_ID)
    if not s or s.get('demo') or s.get('status') == 'cancelled':
        return {'skipped': 'no outbound shipment'}
    if customer_arranged(s):
        return {'skipped': 'customer broker handles customs'}
    async with entity_lock('export-draft:' + s['id']):
        s = await db.raw.shipments.find_one({'id': s['id']}, NO_ID)
        if not s or customer_arranged(s):
            return {'skipped': 'customer broker handles customs'}
        d = await exporter.draft_declaration_for_shipment(s['id'], agent_bot('customs_agent'))
        if d.get('draft_source') != 'agent' or d.get('manual_edit_at'):
            return {'skipped': 'preserve broker-authored declaration'}
        if d['status'] not in ('draft', 'ready', 'rejected'):
            return {'skipped': d['status']}
        invoice = await db.raw.documents.find_one({'shipment_id': s['id'], 'type': 'commercial_invoice', 'extraction.status': 'done'}, NO_ID, sort=[('uploaded_at',-1)])
        packing = await db.raw.documents.find_one({'shipment_id': s['id'], 'type': 'packing_list', 'extraction.status': 'done'}, NO_ID, sort=[('uploaded_at',-1)])
        if not invoice or not packing:
            raise JobDeferred(10, 'Waiting for extracted commercial invoice and packing list')
        goods, source = await data._goods(s, prefer_invoice=True, source_invoice=invoice)
        if not goods or source != 'commercial_invoice':
            raise JobDeferred(10, 'Waiting for invoice goods lines')
        # Match real packing measurements; never apportion a consignment weight
        # across multiple lines by an invented value/quantity share.
        packing_lines = (packing.get('extraction') or {}).get('line_items') or []
        used = set()
        for g in goods:
            pack = customs._match_packing(g['description'], packing_lines, used) or {}
            for key, field in (('net_kg','net_weight_kg'), ('gross_kg','gross_weight_kg'), ('packages','packages'),
                               ('package_type','package_type'), ('marks','marks_and_numbers')):
                if g.get(key) is None:
                    g[key] = pack.get(field)
            if len(goods) == 1:
                for key, field in (('net_kg','total_net_weight_kg'), ('gross_kg','total_gross_weight_kg'),
                                   ('packages','total_packages'), ('package_type','package_type'), ('marks','marks_and_numbers')):
                    if g.get(key) is None:
                        g[key] = customs.field_value(packing, field) or customs.field_value(invoice, field)
        values = {v['seq']: v for v in s.get('export_statistical_values') or [] if v.get('source') == 'customer_statement'}
        if any(g['seq'] not in values for g in goods):
            await customer_message(s['company_id'], s['id'], 'Ilmoita vientierien tilastoarvo euroina Suomen rajalla ja laskentaperuste.',
                'Please state each export goods item statistical value in EUR at the Finnish border and its calculation basis in your customs instructions.',
                key='export-values:' + d['id'])
            raise JobDeferred(10, 'Waiting for exporter border-value statement')
        previous = {(it.get('description'),it.get('origin_country'),it.get('hs_hint')):it for it in d.get('items') or []}
        results = []
        missing = []
        for g in goods:
            old = previous.get((g['description'],g.get('origin_country'),g.get('hs_code'))) or {}
            suggestion = old.get('hs_suggestion') if old.get('cn_code') and hs.cn_entry(old['cn_code']) else None
            results.append(suggestion)
            if not suggestion:
                missing.append(g)
        if missing:
            classified = iter(await hs.classify_items([{'description':g['description'],'origin_country':g.get('origin_country'),'hs_hint':g.get('hs_code')} for g in missing],
                ref={'type':'export_declaration','id':d['id']},company_id=s.get('company_id')))
            results = [result if result else next(classified) for result in results]
        items = []
        floor = float((await get_settings()).get('guard_confidence_floor') or 0.85)
        for g, result in zip(goods, results):
            item = {'seq': g['seq'], 'description': g['description'], 'hs_hint': g.get('hs_code'), 'edited': [],
                    'origin_country': g.get('origin_country'), 'quantity': g.get('quantity'), 'unit': g.get('unit'),
                    'invoice_amount': g.get('amount'), 'statistical_value_cents': round(values[g['seq']]['value_eur'] * 100),
                    'statistical_value_source': values[g['seq']],
                    'gross_kg': g.get('gross_kg'), 'net_kg': g.get('net_kg'), 'packages': g.get('packages'),
                    'package_type': g.get('package_type') or (s.get('cargo') or {}).get('package_type'),
                    'marks': g.get('marks'), 'procedure_code': '1000',
                    'documents': [{'code': 'N380', 'number': customs.field_value(invoice, 'invoice_number')} ]}
            customs.apply_hs_result(item, result)
            confidence, why = item_confidence(item)
            item.update(cn_confirmed=bool(item.get('cn_code') and confidence >= floor), cn_confidence=confidence, cn_confirm_reason=why)
            items.append(item)
        main = next((leg for leg in s.get('legs') or [] if leg.get('role') == 'main'), (s.get('legs') or [{}])[0])
        origin = s.get('origin') or {}
        offices = await customs_offices.offices_for('FI', 'EXP', unlocode=origin.get('unlocode'), mode=main.get('mode'))
        exits = await customs_offices.offices_for('FI', 'EXT', unlocode=(main.get('from') or {}).get('unlocode'), mode=main.get('mode'))
        if len(offices) != 1 and origin.get('lat') is not None and origin.get('lon') is not None:
            try:
                office = await customs_offices.nearest('EXP', origin['lat'], origin['lon'], country='FI')
                offices = [office] if office else []
            except customs_offices.OfficeDataUnavailable:
                offices = []
        location = s.get('goods_location')
        if not location:
            await customer_message(s['company_id'], s['id'], 'Vientiä varten ilmoita tavaroiden esittämispaikan osoite ja kauppatapahtuman luonne.',
                'For export please provide the goods presentation address and transaction nature in the shipment customs instructions.', key='export-instructions:' + d['id'])
            raise JobDeferred(10, 'Waiting for customer presentation-location statement')
        patch = {'items': items, 'consignee': deepcopy((s.get('parties') or {}).get('consignee') or {}),
                 'submitter': {'name': config.COMPANY_NAME, 'email': config.COMPANY_EMAIL}, 'goods_location': location,
                 'transport': {**d.get('transport', {}), 'mode_at_border': customs.MODE_CODE.get(main.get('mode')),
                    'inland_mode': customs.MODE_CODE.get(main.get('mode')), 'containerised': bool((s.get('cargo') or {}).get('containers')),
                    'container_numbers': main.get('container_numbers') or [], 'seal_counts': main.get('seal_counts') or {}},
                 'invoice': {'currency': customs.field_value(invoice, 'currency'), 'total': customs.field_value(invoice, 'total_amount')},
                 'transaction_nature': s.get('transaction_nature'),
                 'delivery_terms': {'incoterm': customs.field_value(invoice, 'incoterm') or s.get('incoterm'),
                     'place': customs.field_value(invoice, 'incoterm_place') or s.get('incoterm_place')},
                 'documents': [{'code': 'N380', 'number': customs.field_value(invoice, 'invoice_number'), 'type': 'commercial_invoice'}]}
        if len(offices) == 1:
            patch['office'] = {'code': offices[0]['reference_number']}
        if (s.get('export_office') or {}).get('source') == 'customer_statement':
            patch['office'] = s['export_office']
        if len(exits) == 1:
            patch['exit_office'] = {'code': exits[0]['reference_number']}
        if main.get('exit_office'):
            patch['exit_office'] = {'code': main['exit_office']}
        d = await exporter.apply_patch(d, patch, agent_bot('customs_agent'))
        questions = [it['hs_suggestion'].get('reason_i18n',{}).get('en') or it['hs_suggestion'].get('reason') for it in items
                     if not it.get('cn_code') and (it.get('hs_suggestion') or {}).get('status') == 'needs_review']
        if any(questions):
            await customer_message(s['company_id'],s['id'],
                'Toimita tarkennettu kauppalasku tai tavaraerittely: ' + '; '.join(str(q) for q in questions if q),
                'Please supply a corrected detailed commercial invoice or goods specification addressing: ' + '; '.join(str(q) for q in questions if q),
                subject='Export goods specification · ' + d['lrn'],
                key='export-spec:' + d['id'] + ':' + hashlib.sha256(str(questions).encode()).hexdigest()[:16])
            return {'waiting':'customer_specification','declaration_id':d['id']}
        check = await exporter.validation_with_offices(d)
        reasons = list(check['reasons'])
        if any(not item['cn_confirmed'] for item in items):
            reasons.append(policy_reason('classification_uncertain','Nimike on vahvistettava.','Classification requires confirmation.'))
        gate = await decide('customs_agent', 'draft_export', subject=('export_declaration', d['id']), company_id=s.get('company_id'),
            inputs={'source': source, 'document_ids': [invoice['id'], packing['id']], 'validation': check},
            decision={'ready': not reasons}, extra_reasons=reasons, approval_kind='document_review', title='Review export ' + d['lrn'])
        if not gate.auto:
            return {'owner_queue': True, 'declaration_id': d['id']}
        d = await exporter.ready(d, agent_bot('customs_agent'))
        d = await exporter.file_declaration(d, agent_bot('customs_agent'))
        return {'declaration_id': d['id'], 'status': d['status']}


@sweeper
async def sweep_exports():
    key = await mode_key()
    count = 0
    async for s in db.raw.shipments.find({'direction':'export','customs_clearance':True,'demo':{'$ne':True},
        'status':{'$nin':['cancelled','delivered']},'created_at':{'$gte':await agents_epoch()}}, NO_ID).limit(100):
        await schedule(s, 'sweep:' + key)
        count += 1
    return count
