import { localizePayload, displayText } from './localizedData';
import { apiErrorMessage } from './apiErrors';
import { aiErrorInfo } from './aiDisplay';

test('display variants never replace API identifiers or enums',()=>{
 const data={id:'same-id',service:'ROAD_LTL',service_i18n:{fi:'Osakuorma',en:'Part load'},services:['ROAD_LTL'],permissions:['shipments.write'],status:'open',status_i18n:{fi:'Avoin',en:'Open'},title:'Old title',title_i18n:{fi:'Otsikko',en:'Title'}};
 const fi=localizePayload(data,'fi');
 expect(fi.id).toBe('same-id');expect(fi.service).toBe('ROAD_LTL');expect(fi.status).toBe('open');expect(fi.services).toEqual(['ROAD_LTL']);expect(fi.permissions).toEqual(['shipments.write']);expect(fi.title).toBe('Otsikko');
 expect(localizePayload(data,'en').title).toBe('Title');expect(data.title).toBe('Old title');
});
test('AI reviewer text and arrays follow the viewer language',()=>{
 const record={ai:{reason:'Reason',reason_i18n:{fi:'Perustelu',en:'Reason'},facts_used:['Fact'],facts_used_i18n:{fi:['Tieto'],en:['Fact']}}};
 expect(localizePayload(record,'fi').ai).toMatchObject({reason:'Perustelu',facts_used:['Tieto']});
 expect(localizePayload(record,'en').ai.facts_used).toEqual(['Fact']);
});
test('legacy templates localise but arbitrary human text and amounts stay intact',()=>{
 const fi=localizePayload({title:'Reply to Suomen Oy: HUO-123',description:'Customer entered English text',amount_cents:12345},'fi');
 expect(fi.title).toBe('Vastaus yritykselle Suomen Oy: HUO-123');
 expect(fi.description).toBe('Customer entered English text');expect(fi.amount_cents).toBe(12345);
 expect(displayText('Accepted, MRN 26FI123 (demo)','fi')).toBe('Hyväksytty, MRN 26FI123 (esimerkki)');
});
test('not found, network and provider failure have Finnish reasons and next steps',()=>{
 expect(apiErrorMessage(404,'Quote not found','fi')).toMatch(/Palaa luetteloon/);
 expect(apiErrorMessage(0,null,'fi')).toMatch(/Tarkista verkkoyhteys/);
 const error=aiErrorInfo({error:'429 concurrent_request_limit'},key=>key);
 expect(error.reason).toBe('review.ai.ratelimit');expect(error.next).toBe('review.ai.ratelimit_next');
});

test('rate buying order keeps units separate and sorts the published low tier',()=>{
 const { compareRateBuy, lowestUnitPrice }=require('./rateSort');
 const a={basis:'PER_KG',tiers:[{unit_cost_cents:22},{unit_cost_cents:3}]};
 const b={basis:'PER_KG',tiers:[{unit_cost_cents:20},{unit_cost_cents:5}]};
 expect(lowestUnitPrice(a)).toBe(3);expect(compareRateBuy(a,b)).toBeLessThan(0);
 expect(compareRateBuy({basis:'PER_CONTAINER',unit_cost_cents:100000},a)).toBeLessThan(0);
 expect(lowestUnitPrice({basis:'PER_KG',unit_cost_cents:0})).toBe(0);
 expect(lowestUnitPrice({basis:'PER_KG',unit_cost_cents:null,tiers:[{unit_cost_cents:null}]})).toBe(Infinity);
});


test('carrier service and live-rail route provenance use the real API display field names',()=>{
 const input={services_note:'Asia–North Europe via Rotterdam with Containerships feeders to Finland.',legs:[{path_note:"Station sequence of the train's own timetable (rata.digitraffic.fi)",mode:'rail'}]};
 const fi=localizePayload(input,'fi');
 expect(fi.services_note).toBe('Aasia–Pohjois-Eurooppa Rotterdamin kautta, Containershipsin syöttöliikenne Suomeen.');
 expect(fi.legs[0].path_note).toBe('Junan oman aikataulun asemat (rata.digitraffic.fi)');
 expect(fi.legs[0].mode).toBe('rail');
});


test('AI history defaults to the active provider and never exposes retired model identifiers',()=>{
 const {aiRunGroups, aiRunLabel}=require('./aiDisplay');
 const active={id:'active',provider:'openai',model:'configured-model'};
 const old={id:'old',provider:'retired-provider',model:'retired-model-id',prompt_preview:'retired-model-id',output_preview:'retired-model-id'};
 const groups=aiRunGroups([old,active,{id:'missing-provider'}],'openai');
 expect(groups.current).toEqual([active]);
 expect(groups.earlier.map(aiRunLabel)).toEqual(['legacy','legacy']);
 expect(groups.earlier[0]).toMatchObject({id:'old',provider:'legacy',model:null,prompt_preview:null,output_preview:null});
 expect(old.model).toBe('retired-model-id');
 expect(aiRunLabel(active)).toBe('configured-model');
 expect(aiRunGroups([old],'openai').current).toEqual([]);
});
