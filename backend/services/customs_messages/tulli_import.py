"""Finnish Customs FI415B H1, import MIG 2.1.7 / XSD 3.0.37."""
from .tulli_common import declaration, serialize, assert_valid
TEMPLATE_VERSION = '3.0.37'

def build_message(decl: dict, environment: str = 'MANUAL') -> str:
    # The environment belongs to the transport, never to the FI415B document.
    return serialize('import', 'FI415B', declaration(decl, 'import'))

def validate(xml):
    return assert_valid(xml, 'import')
