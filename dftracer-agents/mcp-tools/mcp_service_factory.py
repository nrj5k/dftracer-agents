from abc import ABC, abstractmethod
from fastmcp import FastMCP
from typing import Dict, List, Optional


class MCPService(ABC):
     """Abstract base class for all MCP services."""

     @abstractmethod
    def execute(self, data: dict) -> Optional[str]:
          """Execute the service with the provided data.
        
        Args:
            data: The input data for the service.
            
        Returns:
            Optional result from the execution.
         """
        pass

     @property
     @abstractmethod
    def name(self) -> str:
          """Return the name of the service."""
        pass


class MCPServiceFactory:
     """Factory class to manage MCP services."""
     
     _services: Dict[str, "MCPService"] = {}

     @staticmethod
    def register(name: str, service_class: type) -> None:
          """Register an MCP service."""
        MCPServiceFactory._services[name] = service_class()

     @staticmethod
    def get_service(name: str) -> Optional["MCPService"]:
          """Get a registered service by name."""
        return MCPServiceFactory._services.get(name)

     @staticmethod
    def list_services() -> List[str]:
          """List all registered service names."""
        return list(MCPServiceFactory._services.keys())

     @staticmethod
    def get_all_services() -> Dict[str, "MCPService"]:
          """Get all registered services."""
        return MCPServiceFactory._services.copy()


def main():
     """Main entry point for running all registered services."""
    print("Loaded services:", MCPServiceFactory.list_services())
    
    for service_name in MCPServiceFactory.list_services():
        service = MCPServiceFactory.get_service(service_name)
        print(f"Executing {service_name}...")
        result = service.execute({})
        print(f"Result from {service_name}: {result}")


if __name__ == "__main__":
    main()
