import React, { Component, Suspense, lazy } from "react";
import { useNavigate } from "react-router-dom";
import { sharedHeaders } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// s2's 3D globe (MapLibre + deck.gl, ~518 kB gzip) loads only where a map is shown.
const Map3D = lazy(() =>
  Promise.all([import("@huolitsija/shipment-map"), import("@huolitsija/shipment-map/style.css")]).then(([m]) => ({ default: m.ShipmentMap })),
);

const ENDPOINT = `${process.env.REACT_APP_BACKEND_URL || ""}/api/map/shipments`;

class MapBoundary extends Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div role="alert" className="flex h-full items-center justify-center rounded-xl border border-dashed border-[color:var(--border-default)] p-4 text-center text-sm text-ink-secondary">
          {this.props.message}
        </div>
      );
    }
    return this.props.children;
  }
}

/**
 * Live shipment map. scope "customer" shows the caller's company, "ops" every shipment.
 * height: number (px) or any CSS length; the map fills it.
 */
export default function ShipmentMap({ scope, height = 420, initialSelectedId, initialView, className, embedded = false, observations, observationFocus }) {
  const navigate = useNavigate();
  const { locale, t } = useT();
  return (
    <div className={cn("relative overflow-hidden rounded-xl", className)} style={{ height: typeof height === "number" ? Math.max(height, 440) : height, minHeight: 440 }}>
      <MapBoundary message={t("map.failed")}>
        <Suspense fallback={<div className="h-full w-full animate-pulse rounded-xl bg-[var(--bg-subtle)]" aria-label={t("state.loading")} />}>
          <Map3D
            endpoint={ENDPOINT}
            observations={observations}
            observationFocus={observationFocus}
            streamUrl={null}
            pollIntervalMs={30000}
            showDetailPanel={!embedded}
            scope={scope}
            fetchInit={{ credentials: "include" }}
            getHeaders={sharedHeaders}
            onNavigate={(href) => navigate(href)}
            getDetailHref={(s) => `/${scope === "customer" ? "portal" : scope}/shipments/${s.id}`}
            locale={locale === "en" ? "en" : "fi"}
            initialSelectedId={initialSelectedId}
            initialView={initialView}
          />
        </Suspense>
      </MapBoundary>
    </div>
  );
}
