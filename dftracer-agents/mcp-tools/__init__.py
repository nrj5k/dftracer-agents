from .mcp_service_factory import MCPService, MCPServiceFactory


def get_mcp_server():
    """
    Get the registered MCP services and create a unified server.
    
    Returns:
        MCPService: The registered MCP service(s).
    """
    services = MCPServiceFactory.list_services()
    if not services:
        raise ValueError("No MCP services registered.")
    
    combined_server = {}
    for service_name in services:
        service = MCPServiceFactory.get_service(service_name)
        combined_server[service_name] = service
    
    return combined_server


def run_mcp_server():
    """
    Run the MCP service(s) with a unified server interface.
    """
    server = get_mcp_server()
    for name, service in server.items():
        print(f"Starting {name}...")
        # Run the service's main functionality
        result = service.execute({})
        print(f"Result from {name}: {result}")


if __name__ == "__main__":
    run_mcp_server()