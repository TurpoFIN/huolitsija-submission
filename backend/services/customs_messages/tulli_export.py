"""Finnish Customs AES FI515A, export MIG 1.1.1 / XSD 1.0.90."""
from .tulli_common import declaration, serialize, assert_valid
TEMPLATE_VERSION = '1.0.90'

def build_message(decl: dict, environment: str = 'MANUAL') -> str:
    return serialize('export', 'FI515A', declaration(decl, 'export'))

def validate(xml):
    return assert_valid(xml, 'export')
