import json
import faiss
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
INDEX_PATH = Path("data/bamis_ivf.index")
META_PATH = Path("data/bamis_metadata.json")

print("🔌 Loading BAMIS Knowledge Base...")
try:
    # Load Index
    index = faiss.read_index(str(INDEX_PATH))
    # Set nprobe for balance between speed and accuracy (10 is a good start for 100 clusters)
    index.nprobe = 10 
    
    # Load Metadata
    with open(META_PATH, "r", encoding="utf-8") as f:
        METADATA = json.load(f)
        
    # Load Embedding Model
    MODEL = SentenceTransformer('all-MiniLM-L6-v2')
    print(f"✅ BAMIS Tool Ready. Indexed {index.ntotal} vectors.")
except Exception as e:
    print(f"⚠️ Error loading BAMIS data: {e}")
    index = None
    METADATA = []
    MODEL = None

mcp = FastMCP("BAMIS Advisor")

@mcp.tool()
def search_crop_calendar(query: str, k: int = 5) -> str:
    """
    Search the BAMIS agricultural calendar for weather thresholds, crop stages, 
    pest warnings, or favorable conditions.
    
    Args:
        query: A natural language query (e.g., "Tomato rainfall in week 49", "Wheat rust warning Barisal").
        k: Number of results to return (default 5).
    """
    if index is None or MODEL is None:
        return "Error: BAMIS database is not loaded. Please check server logs."

    try:
        # 1. Embed the query
        query_vec = MODEL.encode([query])
        query_array = np.ascontiguousarray(query_vec).astype('float32')
        
        # 2. Search the IVF Index
        distances, indices = index.search(query_array, k)
        
        # 3. Retrieve and format results
        results = []
        for i in indices[0]:
            if 0 <= i < len(METADATA):
                results.append(METADATA[i])
                
        if not results:
            return "No relevant agricultural data found for this query."
            
        return json.dumps(results, indent=2)
        
    except Exception as e:
        return f"Search failed: {str(e)}"

if __name__ == "__main__":
    # For testing the tool locally
    mcp.run()