"""Configuration of the world simulator (read at call time).

DEMO_WORLD_SANDBOX      the boundary flag (read only by boundary.py; see there)
DEMO_WORLD_MODEL        model of the world's actor personas (default LLM_MODEL; OpenAI only)
DEMO_WORLD_SPEED        lifecycle clock speed in simulated seconds per real second (default 1440 = one day per minute)
DEMO_WORLD_REASONING    reasoning effort of the personas (default low)
"""
from core import config


def model() -> str:
    return config.env("DEMO_WORLD_MODEL") or config.LLM_MODEL


def speed() -> float:
    try:
        return max(1.0, float(config.env("DEMO_WORLD_SPEED") or 1440))
    except ValueError:
        return 1440.0


def reasoning() -> str:
    return config.env("DEMO_WORLD_REASONING") or "low"
