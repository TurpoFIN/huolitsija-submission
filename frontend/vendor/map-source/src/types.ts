// Types for the map data contract (shared/map-data-contract.md). Do not extend
// the wire shape here; derived, client-only data lives in `PreparedShipment`.

export type Mode = 'sea' | 'road' | 'air' | 'rail' | 'multimodal';
export type LegMode = Exclude<Mode, 'multimodal'>;
export type Status =
  | 'booked'
  | 'picked_up'
  | 'in_transit'
  | 'at_hub'
  | 'customs_hold'
  | 'out_for_delivery'
  | 'delivered'
  | 'exception';
export type PositionSource = 'adsb' | 'carrier_api' | 'ais' | 'gps' | 'eta_interpolation';
export type Scope = 'customer' | 'ops';

export interface Place {
  name: string;
  unlocode?: string;
  lat: number;
  lon: number;
}

export interface Leg {
  seq: number;
  mode: LegMode;
  from: Place;
  to: Place;
  /** [lon, lat] waypoints along the real lane. */
  path: [number, number][];
  carrier: string;
  etd: string;
  eta: string;
  atd: string | null;
  ata: string | null;
}

export interface Position {
  lat: number;
  lon: number;
  updated_at: string;
  source: PositionSource;
}

export interface Shipment {
  id: string;
  reference: string;
  customer_name: string;
  mode: Mode;
  status: Status;
  origin: Place;
  destination: Place;
  legs: Leg[];
  position: Position;
  progress: number;
  eta: string;
  weight_kg: number | null;
  teu: number | null;
  has_exception: boolean;
  eta_prediction?: {state: string; eta?: string; earliest?: string; latest?: string; confidence?: string; scope?: string; inputs?: {distance_km?: number; speed_kmh?: number; routing?: string}};
}

export interface ShipmentsResponse {
  generated_at: string;
  shipments: Shipment[];
}

/** Payload of the SSE `position` event. */
export interface PositionEvent {
  id: string;
  position: Position;
  progress: number;
  status: Status;
}

export type Connection = 'connecting' | 'live' | 'polling' | 'offline';

export type ThemeName = 'light' | 'dark';
export type Locale = 'en' | 'fi';

export type ViewPreset = 'world' | 'europe' | 'finland';
