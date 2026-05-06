"""Alert emission: postgres `alerts` table + MQTT `alerts/critical` topic.

Critical alerts hit BOTH sinks. Warnings/info hit Postgres only — the MQTT
critical topic is for on-call paging downstream, so we keep its volume low.

Alerts that fail the DB write are queued in a bounded deque and retried on
the next emit. Critical alerts that fail the DB still publish to MQTT —
losing visibility entirely is worse than losing durability for the case
where postgres is the failure being alerted about.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Literal

import aiomqtt
import asyncpg
from batterylab.log import get
from pydantic import BaseModel

log = get("watchdog.alerts")

CRITICAL_TOPIC = "alerts/critical"
RETRY_QUEUE_CAP = 1000

Severity = Literal["info", "warning", "critical"]


class Alert(BaseModel):
    """One alert envelope — same shape as a row in the ``alerts`` table.

    ``message`` is the dedupe slug (e.g. ``"chassis_unreachable"``); the
    EdgeTrigger pattern in ``dedupe.py`` keys off ``(message, chassis,
    channel)`` so the same condition only emits one row per rising edge.
    Both ``chassis_id`` and ``channel_idx`` are optional so chassis-level
    alerts (no specific channel) round-trip cleanly through the schema.
    """

    severity: Severity
    source: str
    message: str
    chassis_id: int | None = None
    channel_idx: int | None = None


class AlertSink:
    """Wraps the asyncpg pool + MQTT client with a retry queue."""

    def __init__(self, pool: asyncpg.Pool, mqtt: aiomqtt.Client) -> None:
        self._pool = pool
        self._mqtt = mqtt
        self._retry: deque[Alert] = deque(maxlen=RETRY_QUEUE_CAP)

    async def emit(self, alert: Alert) -> None:
        """Persist + (if critical) publish, with a bounded retry queue.

        Critical alerts always attempt the MQTT publish even when the DB
        insert fails — losing visibility entirely is worse than losing
        durability when Postgres is itself the thing being alerted on.
        """
        # Drain retry queue first — preserves rough ordering.
        while self._retry:
            queued = self._retry[0]
            if not await self._insert_db(queued):
                break
            self._retry.popleft()

        db_ok = await self._insert_db(alert)
        if not db_ok:
            self._retry.append(alert)

        if alert.severity == "critical":
            try:
                await self._mqtt.publish(
                    CRITICAL_TOPIC,
                    json.dumps(alert.model_dump(exclude_none=False)),
                    qos=1,
                    retain=False,
                )
            except aiomqtt.MqttError as e:
                log.warning("alert_mqtt_publish_failed", message=alert.message, error=str(e))

    async def _insert_db(self, alert: Alert) -> bool:
        """Best-effort INSERT into ``alerts``. Returns False on any
        Postgres error so ``emit`` can re-queue for later retry."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO alerts (severity, source, message, chassis_id, channel_idx)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    alert.severity,
                    alert.source,
                    alert.message,
                    alert.chassis_id,
                    alert.channel_idx,
                )
            return True
        except asyncpg.PostgresError as e:
            log.warning("alert_db_insert_failed", message=alert.message, error=str(e))
            return False
