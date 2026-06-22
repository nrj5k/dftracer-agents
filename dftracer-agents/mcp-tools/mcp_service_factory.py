from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type, Union


class MCPService(ABC):
    """Abstract base class for all MCP services."""

    @abstractmethod
    def execute(self, data: dict) -> Optional[str]:
        """Execute the service with the provided data."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the service name."""


class MCPServiceFactory:
    """Factory class to register and retrieve MCP services."""

    _services: Dict[str, MCPService] = {}

    @classmethod
    def register(
        cls,
        name: str,
        service: Union[MCPService, Type[MCPService]],
    ) -> MCPService:
        """Register a service instance or service class under ``name``."""
        instance = service() if isinstance(service, type) else service
        cls._services[name] = instance
        return instance

    @classmethod
    def get_service(cls, name: str) -> Optional[MCPService]:
        """Get a registered service by name."""
        return cls._services.get(name)

    @classmethod
    def list_services(cls) -> List[str]:
        """List all registered service names."""
        return list(cls._services.keys())

    @classmethod
    def get_all_services(cls) -> Dict[str, MCPService]:
        """Get a copy of all registered services."""
        return cls._services.copy()


def main() -> None:
    """Small debug entrypoint to inspect registered services."""
    print("Loaded services:", MCPServiceFactory.list_services())


if __name__ == "__main__":
    main()
