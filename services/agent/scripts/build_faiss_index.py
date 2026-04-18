import json
import faiss
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer

def main():
    # Paths
    data_path = Path("../mcp_weather/data/agri_data/vector_ready_chunks.jsonl")
    index_path = Path("../mcp_weather/data/agri_data/bamis_ivf.index")
    meta_path = Path("../mcp_weather/data/agri_data/bamis_metadata.json")

    print("📚 Loading chunks...")
    texts = []
    metadatas = []

    if not data_path.exists():
        print(f"❌ Error: File not found at {data_path}")
        return

    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            chunk = json.loads(line)
            texts.append(chunk['text'])
            metadatas.append(chunk.get('meta', chunk.get('metadata', {})))

    if not texts:
        print("⚠️ No text chunks found. Aborting index build.")
        return

    print("🧠 Generating Embeddings...")
    model = SentenceTransformer('all-MiniLM-L6-v2')
    embeddings = model.encode(texts, show_progress_bar=True)
    
    # Ensure float32 and contiguous memory
    xb = np.ascontiguousarray(embeddings).astype('float32')
    dimension = xb.shape[1]
    total_vectors = xb.shape[0]

    print(f"🏗️ Building IVF Index (Dimension: {dimension}, Vectors: {total_vectors})...")
    
    # 1. Define the Coarse Quantizer (The "Centroid" finder)
    # We use a simple FlatL2 index as the quantizer for the IVF
    nlist = 100  # Number of clusters (Voronoi cells). Rule of thumb: sqrt(N) to 4*sqrt(N)
    if total_vectors < 1000:
        nlist = 10 # Smaller dataset needs fewer clusters
        
    quantizer = faiss.IndexFlatL2(dimension)
    
    # 2. Create the IVF Index
    index = faiss.IndexIVFFlat(quantizer, dimension, nlist, faiss.METRIC_L2)
    
    # 3. Train the index
    # IVF needs to learn the cluster centers from the data first
    print(f"   Training index on {min(10000, total_vectors)} vectors...")
    index.train(xb)
    
    # 4. Add vectors to the index
    print("   Adding vectors to index...")
    index.add(xb)
    
    # 5. Set nprobe for search balance (Speed vs Accuracy)
    # Higher nprobe = more accurate but slower. Start with sqrt(nlist)
    index.nprobe = int(np.sqrt(nlist))
    
    print(f"✅ Index built. Total vectors: {index.ntotal}")
    print(f"   nlist (clusters): {nlist}")
    print(f"   nprobe (search depth): {index.nprobe}")

    # Save artifacts
    faiss.write_index(index, str(index_path))
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadatas, f)

    print(f"✅ Index saved to {index_path}")
    print(f"📂 Metadata saved to {meta_path}")

if __name__ == "__main__":
    main()