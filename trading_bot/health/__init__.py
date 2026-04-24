"""Health check HTTP endpoint for external monitoring."""

from .server import HealthServer

__all__: list[str] = ["HealthServer"]
