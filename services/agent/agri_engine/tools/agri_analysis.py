from mcp.server.fastmcp import FastMCP
from ..processor import AgriProcessor
import os

# Initialize processor
processor = AgriProcessor(gee_project=os.getenv("GEE_PROJECT_ID"))

def register_agri_tools(mcp_server: FastMCP):
    
    @mcp_server.tool()
    def get_field_analysis(lat: float, lon: float) -> str:
        """
        Delineates field boundaries and analyzes crops using Agribound.
        """
        try:
            result = processor.process_field(lat, lon)
            return (f"Field analysis successful. Found {result['field_count']} fields. "
                    f"Boundaries exported to: {result['boundaries_file']}")
        except Exception as e:
            return f"Analysis failed: {str(e)}"