# Included map source

This directory preserves the editable source and locked build inputs for the current `@huolitsija/shipment-map` package, version `1.1.2-ci.1`. The host app installs the adjacent `huolitsija-shipment-map-1.1.2-ci.1.tgz` archive so a normal frontend installation needs no separate map build.

The upstream README contains the component API and design notes. Its references to the original demo site, fixture harness, shared workspace and older package versions are historical. Only `src/`, library build configuration and dependency locks are included here.

Run `npm ci` and `npm run build:lib` in this directory to rebuild the library. MapLibre, deck.gl and Lucide retain their licenses, and map-provider attribution must remain visible.
