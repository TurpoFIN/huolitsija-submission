"""Controls reference routes precede the declaration-id route in server.py."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from core.deps import require_perm
from core.db import db
from core.util import NO_ID
from services import dual_use, customs_screening

router = APIRouter(prefix='/api/customs', tags=['customs-controls'])

@router.get('/dual-use/search')
async def search(q: str = Query(..., min_length=1), user=Depends(require_perm('customs.review'))):
    return {'results': dual_use.search(q), 'source': dual_use.data('annex1')['source']}

@router.get('/dual-use/entry/{code}')
async def entry(code: str, user=Depends(require_perm('customs.review'))):
    result = dual_use.entry(code)
    if not result:
        raise HTTPException(404, {'fi': 'Valvontakoodia ei löydy.', 'en': 'Control entry not found.'})
    return result

@router.get('/dual-use/assess')
async def assess(code: str, destination: str = Query(..., min_length=2, max_length=2),
                 flow: Literal['import', 'export', 'transit', 'transfer', 'ens'] = 'export',
                 user=Depends(require_perm('customs.review'))):
    item = {'cn_code': code} if code.isdigit() else {'dual_use_code': code}
    return await dual_use.assess(item, flow=flow, destination=destination)

@router.post('/{decl_id}/screen')
async def screen(decl_id: str, user=Depends(require_perm('customs.review'))):
    # Preserve the existing import endpoint response and tenant authorization.
    from routes.customs import _get, get_declaration
    from services import customs
    d = await _get(decl_id, user)
    result = await customs_screening.screen_parties(d, kind='import')
    await db.customs_declarations.update_one({'id': decl_id}, {'$set': {'screening': result}})
    d = await db.customs_declarations.find_one({'id': decl_id}, NO_ID)
    if d['status'] in customs.EDITABLE_BROKER:
        await customs.save_and_recompute(d, user, 'screened')
    return await get_declaration(decl_id, user)
