# @huolitsija/shipment-map

Editable TypeScript source for the portal and operations shipment map, version `1.1.2-ci.1`. The host installs the adjacent prebuilt package archive. React and React DOM are external peers; MapLibre, deck.gl and Lucide are bundled.

Run `npm ci`, then `npm run build:lib` in this directory to rebuild the library. `npm run typecheck` checks source types. This export includes the library source and build inputs; the original demo site and browser-test harness are omitted.

Import `@huolitsija/shipment-map/style.css` once, then mount `ShipmentMap` inside a sized container. Core props are `endpoint` (snapshot URL), `scope` (`customer` or `ops`), `locale` (`fi` or `en`), `theme`, `getHeaders`, and `onNavigate`. `streamUrl` selects the SSE feed; the client falls back to polling. See `src/types.ts` for the full contract and `src/feed.ts` for feed behavior.

Map-provider attribution must remain visible. The default styles use OpenFreeMap/OpenMapTiles/OpenStreetMap. MapLibre GL JS is BSD-3-Clause, deck.gl is MIT and Lucide is ISC; the application component is unlicensed. See the repository's `THIRD_PARTY_NOTICES.md`.
