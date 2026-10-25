"""Imports the demo seeders so their startup hooks register (core seed runs first, order 10)."""
from fastapi import APIRouter

import services.locale_backfill  # noqa: F401
import services.seed_core  # noqa: F401

router = APIRouter(prefix="/api", tags=["seed"])

import services.domain_data_repair  # idempotent generated-data correction, startup order 65
import services.data_repair_s4  # one-time leg-stamp and demo-name corrections after the s4 journey, orders 67-69
import services.data_repair_r3  # one-time withdrawal of quotes that charge no freight (r3 N1), order 70
import services.notification_backfill  # noqa: F401  one-time FI/EN variants for stored notifications, order 71
import services.data_repair_r3b  # one-time cleanup of the 765ad42 double startup run; document use resync, order 71
import services.data_repair_demo_k6  # one-time: demo shipment K6 gets documents of its own goods (r3), order 72

import services.autopilot  # registers bounded agent queue processing
