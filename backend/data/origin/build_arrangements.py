"""Build source-derived catalogue and Annex B/TARIC codes from hash-checked official captures.
Proof policy annotations below cite the captured protocol, and are deliberately limited to reviewed schemes.
Run scripts/origin_capture.py and scripts/origin_reference_capture.py first. No network during this build.
"""
import gzip,hashlib,json,re
from pathlib import Path
from lxml import html,etree
D=Path(__file__).resolve().parent
M=json.loads((D/'manifest.json').read_text())
def raw(n):
    b=gzip.decompress((D/M[n]['file']).read_bytes());assert hashlib.sha256(b).hexdigest()==M[n]['sha256'];return b
def obj(n):return json.loads(raw(n))
def text(n):return html.fromstring(raw(n).decode('utf-8')).text_content()
def protocol(c,lang='en'):
    return html.fromstring(obj(f'protocol_{c}_{lang}')['bodyContent'].replace('\\"','"').encode(), parser=html.HTMLParser(encoding='utf-8'))
def lines(tree):return [' '.join(p.text_content().split()) for p in tree.xpath('//p')]
def proof(typ,months=12,who=None,**kw):return dict(type=typ,validity_months=months,**({'who':who} if who else {}),**kw)
# Import direction. EU exporters' REX rules are not applied to Japan/Canada/UK exporters.
POLICY={
 'JP':[proof('statement_on_origin',who='japan_exporter'),proof('importers_knowledge',None)],
 'VN':[proof('eur1'),proof('origin_declaration',who='any_exporter',max_value_eur=6000)],
 'KR':[proof('origin_declaration',who='approved_exporter'),proof('origin_declaration',who='any_exporter',max_value_eur=6000)],
 'CA':[proof('origin_declaration',who='canadian_exporter')],
 'GB':[proof('statement_on_origin',who='uk_exporter'),proof('importers_knowledge',None)],
 'GSPB':[proof('statement_on_origin_rex',who='registered_exporter'),proof('statement_on_origin',who='any_exporter',max_value_eur=6000)],
 'GSPA':[proof('statement_on_origin_rex',who='registered_exporter'),proof('statement_on_origin',who='any_exporter',max_value_eur=6000)],
 'PEM2021':[proof('eur1',10),proof('origin_declaration',10,who='approved_exporter'),proof('origin_declaration',10,who='any_exporter',max_value_eur=6000)],
 'EEA2022':[proof('eur1',10),proof('origin_declaration',10,who='approved_exporter'),proof('origin_declaration',10,who='any_exporter',max_value_eur=6000)],
}
for scheme, months in [('CAM',12),('CARIFORUM',10),('CPE',12),('MX',10),('PACIFIC',10),('SADC',10),('PEM',4)]:
    POLICY[scheme]=[proof('eur1',months),proof('origin_declaration',months,who='approved_exporter'),
                    proof('origin_declaration',months,who='any_exporter',max_value_eur=6000)]
POLICY['PEM'] += [proof('eur_med',4),proof('origin_declaration_eur_med',4,who='approved_exporter')]
for scheme in ['NZ','CL','MERCOSUR']:
    POLICY[scheme]=[proof('statement_on_origin',12,who='national_exporter')]
for scheme in ['NZ','CL']:
    POLICY[scheme].append(proof('importers_knowledge',None))
POLICY['SG']=[proof('statement_on_origin',12,who='national_registered_exporter')]
POLICY['GH']=[proof('origin_declaration',10,who='national_registered_exporter'),
              proof('origin_declaration',10,who='any_exporter',max_value_eur=6000)]
REP={'CAM':'CR','CARIFORUM':'DO','CPE':'CO','MX':'MX','PACIFIC':'PG','SADC':'ZA','PEM':'DZ',
     'NZ':'NZ','CL':'CL','MERCOSUR':'BR','SG':'SG','GH':'GH','JP':'JP','VN':'VN','KR':'KR','CA':'CA','GB':'GB','GSPB':'IN','GSPA':'BD','PEM2021':'CH','EEA2022':'NO'}

def statement(c,lang):
    tree=protocol(c,lang); pp=tree.xpath('//p')
    # The TCA uses a shared English fallback block in the EN text. FI has its own published text.
    if c=='GB':
        candidates=[p for p in pp if ('The exporter of the products covered' in p.text_content() if lang=='en' else 'Tässä asiakirjassa mainittujen tuotteiden viejä' in p.text_content())]
    elif c in ('IN','BD','PK'):
        # Annex 22-07 prescribes English, French or Spanish; do not invent Finnish wording.
        if lang=='fi':return None
        candidates=[p for p in pp if 'Number of Registered Exporter' in p.text_content() and p.text_content().lstrip().startswith('The exporter')]
    else:
        phrase='The exporter of the products covered' if lang=='en' else 'Tässä asiakirjassa mainittujen tuotteiden viejä'
        candidates=[p for p in pp if phrase in p.text_content()]
    if not candidates:return None
    p=candidates[0]
    # Remove numbered footnote references only; preserve the official wording.
    s=' '.join(p.text_content().split())
    s=re.sub(r'\s*\(\d+\)', '', s)
    s=re.sub(r'…|\.{3,}', '…', s)
    if c in ('IN','BD','PK'):
        s=s.replace('… (Number of Registered Exporter)', '{exporter_id} (Number of Registered Exporter)',1)
        s=s.replace('… preferential', '{origin_country} preferential',1)
        s=re.sub('…(?: …)?', '{origin_criterion}',s)
    else:
        s=s.replace('…','{exporter_id}',1).replace('…','{origin_country}',1)
    return {'text':s,'source_url':M[f'protocol_{c}_{lang}']['url'],
            'additional_fields':['origin_criterion','place','date','exporter_name'] if c=='JP' else ['place','date','exporter_name'],
            'draft':True}

locales={l:obj('locale_'+l) for l in ('en','fi')}
partners={n.removeprefix('variants_'):[r['variantDescription'] for r in obj(n)] for n in M if n.startswith('variants_')}
arrangements={}
for code in sorted({c for sets in partners.values() for c in sets}):
    ps=[c for c,ss in partners.items() if code in ss]
    names={l:locales[l].get('agreement',{}).get('name',{}).get(code) for l in locales}
    typ='eba' if code=='GSPA' else 'gsp' if code=='GSPB' else 'pem' if code.startswith(('PEM','EEA')) else 'fta'
    for l,n in names.items():
        if isinstance(n,dict): names[l]=' / '.join(dict.fromkeys(n.values()))
    names['fi'] = {'JP':'EU–Japani-talouskumppanuussopimus','VN':'EU–Vietnam-vapaakauppasopimus',
      'KR':'EU–Etelä-Korea-vapaakauppasopimus','CA':'EU–Kanada CETA-sopimus','GB':'EU–Yhdistynyt kuningaskunta TCA-sopimus',
      'GSPA':'Kaikki paitsi aseet (EBA)','GSPB':'Yleinen tullietuusjärjestelmä (GSP)',
      'PEM2021':'Välimeren alueen alkuperäsäännöt (PEM)','EEA2022':'Euroopan talousalueen alkuperäsäännöt'}.get(code,names['fi'] or names['en'])
    rep=REP.get(code)
    arrangements[code]={'name_en':names['en'] or code,'name_fi':names['fi'] or names['en'] or code,'type':typ,'partners':ps,
      'proofs':POLICY.get(code,[]),'proof_policy_reviewed':code in POLICY,
      'source_url':M[f'protocol_{rep}_en']['url'] if rep else M['locale_en']['url'],
      'legal_basis':{'source':'Access2Markets published origin protocol','partner':rep} if rep else None,
      'statement_text':{l:statement(rep,l) for l in locales} if rep else {}}
# Customs-union membership is published in DG TAXUD's arrangements list; TARIC determines product coverage.
taxud=text('customs_unions').lower()
for c,en,fi in [('TR','Türkiye','Turkki'),('AD','Andorra','Andorra'),('SM','San Marino','San Marino')]:
    assert en.lower() in taxud or (c=='TR' and 'turkey' in taxud)
    arrangements['CU_'+c]={'name_en':'EU–'+en+' customs union','name_fi':'EU–'+fi+'-tulliliitto','type':'customs_union','partners':[c],
       'source_url':M['customs_unions']['url'],'proofs':[proof('atr',4)] if c=='TR' else [],
       'proof_policy_reviewed':c=='TR','statement_text':{}}
# Group IDs are the live TARIC geography identifiers. Matching live measures remains mandatory.
groups={'2020':{'arrangement':'GSPB','type':'gsp'},'2027':{'arrangement':'GSPB','type':'gsp_plus'},'2005':{'arrangement':'GSPA','type':'eba'}}
out={'generated_at':max(v['fetched_at'] for v in M.values()),'arrangements':arrangements,'partners':partners,'taric_groups':groups,
     'country_names':{l:{'EU':locales[l]['countryNames']['EU'],**locales[l]['countryNames']['nonMembersEU'],**locales[l]['countryNames']['membersEU']} for l in locales},'sources':list(M.values())}
(D/'arrangements.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
# Annex B uses grid-list pairs; the final Preference section contains both components.
pc={'first_digit':{},'suffix':{}}
for lang in locales:
 tree=html.fromstring(raw('annex_b_'+lang))
 pairs=[]
 for d in tree.xpath('//div[contains(@class,"grid-list")]'):
    a=d.xpath('./div[contains(@class,"grid-list-column-1")]');b=d.xpath('./div[contains(@class,"grid-list-column-2")]')
    if a and b:pairs.append((' '.join(a[0].text_content().split()),' '.join(b[0].text_content().split())))
 # Identify the first component by the stable official phrase, then take the consecutive code blocks.
 start=next(i for i,(c,t) in enumerate(pairs) if c=='1' and ('Tariff arrangement erga omnes' in t if lang=='en' else 'Tavanomainen tullikohtelu' in t))
 for c,t in pairs[start:start+16]:
    key='first_digit' if len(c)==1 else 'suffix'
    if re.fullmatch(r'\d{1,2}',c):pc[key].setdefault(c,{})[lang]=t
# Semantic mapping annotates real CSRD2 codes; descriptions come verbatim from the official extract.
meta={
 'N954':('eur1',[]),'N018':('atr',['CU_TR']), 'U045':('eur_med',[]),'U048':('origin_declaration_eur_med',[]),
 'N864':('origin_declaration',['KR','CA','VN','CAM','CARIFORUM','CPE','MX','PACIFIC','SADC','PEM','GH']), 'U162':('origin_declaration',['KR','CA','VN']),
 'U075':('eur1',['PEM2021','EEA2022']),'U076':('origin_declaration',['PEM2021','EEA2022']),
 'U110':('statement_on_origin',['JP']),'U111':('statement_on_origin',['JP']),'U112':('importers_knowledge',['JP']),
 'U116':('statement_on_origin',['GB']),'U117':('importers_knowledge',['GB']),'U118':('statement_on_origin',['GB']),
 'U101':('statement_on_origin',['SG']), 'U120':('statement_on_origin',['NZ']), 'U122':('importers_knowledge',['NZ']),
 'U123':('statement_on_origin',['CL']), 'U125':('importers_knowledge',['CL']), 'U126':('statement_on_origin',['MERCOSUR']),
 'U164':('statement_on_origin_rex',['GSPB','GSPA']),'U165':('statement_on_origin_rex',['GSPB','GSPA']),'U166':('statement_on_origin',['GSPB','GSPA'])}
docs={};root=etree.fromstring(raw('cci_documents'))
for entry in root:
 codes=entry.xpath('./*[local-name()="dataItem"]'); code=codes[0].text if codes else None
 if code not in meta:continue
 desc={d.get('lang'):d.text for d in entry.xpath('.//*[local-name()="description"]')}
 docs[code]={'proof_type':meta[code][0],'arrangements':meta[code][1],'en':desc.get('en'),'fi':desc.get('fi'),'source':M['cci_documents']['url']}
assert set(meta)<=set(docs)
assert pc['first_digit']['4']['en'].startswith('Customs') and 'quota' in pc['suffix']['20']['en'].lower()
(D/'codes.json').write_text(json.dumps({'preference_codes':pc,'document_codes':docs,'sources':[M[n] for n in ['annex_b_en','annex_b_fi','cci_documents']]},ensure_ascii=False,indent=2)+'\n')
readme=['# Official origin data','', 'Build: `python scripts/origin_capture.py`, `python scripts/origin_reference_capture.py`, then `python backend/data/origin/build_arrangements.py`.', '',
 'Raw responses are gzip compressed without modifying their bytes. The CCI XML is an explicitly recorded entity extraction. SHA256 values refer to decompressed bytes. `manifest.json` contains every URL, retrieval time, extraction and download hash.', '',
 'Proof policies are reviewed annotations of the downloaded agreements. Unreviewed arrangements expose their official name and partner mapping but cannot claim preference. Import and EU-export exporter-registration requirements differ. Statement output is a draft: mandatory shipment fields must be completed.', '',
 '| Capture | Source URL | Retrieved UTC | SHA256 |','|---|---|---|---|']
readme += [f"| {n} | {v['url']} | {v['fetched_at']} | {v['sha256']} |" for n,v in sorted(M.items())]
(D/'README.md').write_text('\n'.join(readme)+'\n')
print('built',len(partners),'partners',len(arrangements),'arrangements',len(docs),'document codes')
