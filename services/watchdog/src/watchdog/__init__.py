"""watchdog — observable failures (orchestrator silent, chassis tripped,
chamber off-setpoint) become durable alerts in Postgres + an MQTT topic.

Per CLAUDE.md invariant #1 ("safety in hardware, not Python"), this service
NEVER halts cells. It only emits alerts. The cycler safety loop remains the
sole actuator.
"""
