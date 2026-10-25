"""Odoo 18 Community XML-RPC stock.picking adapter (official external API).

Creates stock operations and reads warehouse-confirmed done status. It never validates
physical stock moves itself. Product/UoM/location IDs are configured warehouse mappings.
"""
import asyncio
import ssl
import xmlrpc.client
from core import config

class TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        conn = super().make_connection(host); conn.timeout = 25; return conn
class TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        conn = super().make_connection(host); conn.timeout = 25; return conn

def proxy(path):
    url = config.env('WMS_ODOO_URL').rstrip('/')
    if not url: raise ValueError('WMS_ODOO_URL is missing')
    return xmlrpc.client.ServerProxy(url + path, allow_none=True,
        transport=TimeoutSafeTransport() if url.startswith('https://') else TimeoutTransport())

def call(model, method, args, kwargs=None):
    database, user, key = (config.env('WMS_ODOO_' + n) for n in ('DB', 'USER', 'API_KEY'))
    if not all((database, user, key)): raise ValueError('Odoo credentials are not configured')
    uid = proxy('/xmlrpc/2/common').authenticate(database, user, key, {})
    if not uid: raise ValueError('Odoo authentication failed')
    return proxy('/xmlrpc/2/object').execute_kw(database, uid, key, model, method, args, kwargs or {})

def _send(order, warehouse):
    operation = warehouse.get('odoo_' + order['kind'] + '_type_id')
    product, uom = warehouse.get('odoo_product_id'), warehouse.get('odoo_uom_id')
    if not all((operation, product, uom)): raise ValueError('Warehouse Odoo operation, product and UoM mappings are required')
    # Deterministic origin lets a retry recover an operation after an HTTP timeout.
    origin = 'HUOLITSIJA:' + order['id']
    found = call('stock.picking', 'search_read', [[['origin', '=', origin]]], {'fields': ['id', 'name', 'state'], 'limit': 2})
    if len(found) > 1: raise ValueError('Duplicate Odoo origin; reconciliation required')
    if found: return found[0]
    op = call('stock.picking.type', 'read', [[operation]], {'fields': ['default_location_src_id', 'default_location_dest_id']})[0]
    src, dst = op['default_location_src_id'], op['default_location_dest_id']
    if not src or not dst: raise ValueError('Odoo operation needs source and destination locations')
    values = {'origin': origin, 'picking_type_id': operation, 'location_id': src[0], 'location_dest_id': dst[0],
        'move_ids_without_package': [[0, 0, {'name': order['reference'], 'product_id': product, 'product_uom': uom,
            'product_uom_qty': order['packages'], 'location_id': src[0], 'location_dest_id': dst[0]}]]}
    pid = call('stock.picking', 'create', [values])
    call('stock.picking', 'action_confirm', [[pid]])
    return call('stock.picking', 'read', [[pid]], {'fields': ['id', 'name', 'state']})[0]

async def send(order, warehouse): return await asyncio.to_thread(_send, order, warehouse)
async def status(external_id):
    def read():
        rows = call('stock.picking', 'read', [[external_id]], {'fields': ['id', 'name', 'state', 'date_done', 'move_ids']})
        if not rows: raise ValueError('Odoo stock operation no longer exists')
        row = rows[0]
        moves = call('stock.move', 'read', [row['move_ids']], {'fields': ['quantity']}) if row['move_ids'] else []
        row['packages'] = sum(m['quantity'] for m in moves)
        return row
    return await asyncio.to_thread(read)
