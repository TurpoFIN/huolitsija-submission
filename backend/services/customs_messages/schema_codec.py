"""Strict schema-ordered XML encoding. No inferred or fabricated business values.

Input dictionaries use the official XML element names. Lists preserve repetitions;
@name sets attributes. Unknown fields are rejected, and every output is XSD validated.
"""
from functools import lru_cache
from pathlib import Path
from lxml import etree as E

DATA = Path(__file__).resolve().parents[2] / 'data' / 'xsd'
X = '{http://www.w3.org/2001/XMLSchema}'


def parse_xml(xml):
    raw = xml.encode() if isinstance(xml, str) else xml
    if len(raw) > 8_000_000:
        raise ValueError('XML exceeds 8 MB')
    root = E.fromstring(raw, E.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_comments=True))
    if root.getroottree().docinfo.doctype:
        raise ValueError('DOCTYPE is not permitted')
    return root


@lru_cache(maxsize=160)
def schema(path):
    return E.XMLSchema(E.parse(str(path)))


def validate(xml, path):
    root = parse_xml(xml)
    schema(path).assertValid(root)
    return root


@lru_cache(maxsize=8)
def definitions(directory):
    return {e.get('name'): e for f in Path(directory).glob('*.xsd')
            for e in E.parse(str(f)).getroot() if e.get('name') and e.tag != X+'element'}


def children(node, defs):
    for e in node:
        if e.tag == X + 'element':
            yield e
        elif e.tag == X + 'group':
            yield from children(defs[e.get('ref').split(':')[-1]], defs)
        elif e.tag in {X+'sequence', X+'choice', X+'all', X+'complexContent', X+'extension'}:
            if e.get('base') and e.get('base').split(':')[-1] in defs:
                yield from children(defs[e.get('base').split(':')[-1]], defs)
            yield from children(e, defs)


def encode(path, message, payload):
    path = Path(path)
    doc = E.parse(str(path)).getroot()
    namespace = doc.get('targetNamespace')
    qualified = doc.get('elementFormDefault') == 'qualified'
    defs = definitions(str(path.parent))
    root = E.Element(E.QName(namespace, message), nsmap={'m': namespace})

    def fill(node, value, typ):
        if not isinstance(value, dict):
            if value is None:
                raise ValueError(f'{node.tag}: missing value')
            node.text = str(value)
            return
        spec = defs.get((typ or '').split(':')[-1])
        if spec is None:
            raise ValueError(f'No complex type for {node.tag}')
        fields = list(children(spec, defs))
        known = {e.get('name') for e in fields} | {'#text'}
        unknown = {k for k in value if not k.startswith('@')} - known
        if unknown:
            raise ValueError(f'{node.tag}: unknown fields {sorted(unknown)}')
        if '#text' in value:
            node.text = str(value['#text'])
        for k, v in value.items():
            if k.startswith('@'):
                node.set(k[1:], str(v))
        for field in fields:
            name = field.get('name')
            if name not in value:
                continue
            values = value[name] if isinstance(value[name], list) else [value[name]]
            for v in values:
                child = E.SubElement(node, E.QName(namespace, name) if qualified else name)
                fill(child, v, field.get('type'))
    root_spec = doc.find(f"{X}element[@name='{message}']")
    fill(root, payload, root_spec.get('type'))
    raw = E.tostring(root, encoding='UTF-8', xml_declaration=True)
    validate(raw, path)
    return raw.decode()


def as_dict(node):
    if not len(node):
        return node.text or ''
    out = {}
    for c in node:
        k = E.QName(c).localname
        v = as_dict(c)
        if k in out:
            if not isinstance(out[k], list): out[k] = [out[k]]
            out[k].append(v)
        else:
            out[k] = v
    return out
