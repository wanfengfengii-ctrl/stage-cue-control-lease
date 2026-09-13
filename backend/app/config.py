import os


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # Defaults match docker-compose's postgres service.
    return (
        "postgresql://"
        f"{os.environ.get('POSTGRES_USER', 'stage')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'stage')}@"
        f"{os.environ.get('POSTGRES_HOST', 'db')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/"
        f"{os.environ.get('POSTGRES_DB', 'stage')}"
    )


DATABASE_URL = _database_url()

# Fixed lease lifetime; the lease is considered expired as soon as the
# server-side UTC clock reaches expires_at (inclusive boundary).
LEASE_TTL_SECONDS = 30

# Short polling interval suggested to the UI.
POLL_INTERVAL_MS = 1000

# Dangerous actions shared by the lifting platform and the flying hoist.
# Each action can have at most one valid lease at any instant.
ACTION_IDS = [
    "lift_up",
    "lift_down",
    "hoist_fly_in",
    "hoist_fly_out",
    "emergency_stop",
]

ACTION_LABELS = {
    "lift_up": "升降台 上升",
    "lift_down": "升降台 下降",
    "hoist_fly_in": "飞行吊点 进场",
    "hoist_fly_out": "飞行吊点 退场",
    "emergency_stop": "紧急停止（联排）",
}

# Physical device each dangerous action belongs to. Linked execution is a
# coordination BETWEEN devices: the lifting platform and the flying hoist move
# together. Two directions of the SAME device (e.g. lift_up + lift_down) are
# mutually exclusive and can never form a linked pair; emergency_stop is a
# standalone interlock and never participates either.
DEVICE_OF = {
    "lift_up": "lift",
    "lift_down": "lift",
    "hoist_fly_in": "hoist",
    "hoist_fly_out": "hoist",
}

# Only pairs spanning two different devices are valid linked combinations.
LINKABLE_DEVICE_PAIRS = {frozenset(("lift", "hoist"))}
