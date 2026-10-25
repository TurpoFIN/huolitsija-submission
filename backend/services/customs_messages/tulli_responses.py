"""Tulli business responses. Codes are from official import/export MIG use cases."""
from lxml import etree
from .tulli_common import parse, assert_valid, NAMESPACES

EVENTS = {'FI428B':'accepted', 'FI416B':'rejected', 'FI444B':'under_control', 'FI429B':'released',
          'FI410B':'invalidated', 'FI528A':'accepted', 'FI516A':'rejected', 'FI544A':'under_control',
          'FI551A':'rejected', 'FI529A':'released', 'FI599A':'exited', 'FI510A':'invalidated'}


def parse_response(xml, system):
    root = parse(xml)
    if etree.QName(root).namespace != NAMESPACES[system]:
        raise ValueError('Unexpected Tulli business message namespace')
    validation = assert_valid(xml, system)
    kind = etree.QName(root).localname
    ns = {'t': NAMESPACES[system]}
    refs = root.xpath('./t:FunctionalReference[@function="MRN"]/text()', namespaces=ns)
    lrns = root.xpath('./t:FunctionalReference[@function="LRN"]/text()', namespaces=ns)
    from services.tulli_codelists import error_text
    errors = []
    for n in root.xpath('.//t:Error', namespaces=ns):
        code = n.findtext('t:Code', namespaces=ns)
        reason = n.findtext('t:Reason', namespaces=ns)
        texts = error_text(reason or code)
        errors.append({'code': code, 'reason': reason, 'text': n.findtext('t:Description', namespaces=ns),
                       'message_fi': texts['fi'], 'message_en': texts['en'], 'message_i18n': texts})
    return {'type': EVENTS.get(kind), 'raw_type':kind, 'mrn':refs[0] if refs else None,
            'lrn':lrns[0] if lrns else None, 'validation':validation,
            'notification': {'text': root.findtext('t:Notification/t:Text', namespaces=ns),
                             'request_correlation': root.findtext('t:Notification/t:RequestCorrelation', namespaces=ns),
                             'type': root.findtext('t:Notification/t:Type', namespaces=ns)},
            'errors': errors}
