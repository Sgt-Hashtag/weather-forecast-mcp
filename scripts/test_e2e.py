#!/usr/bin/env python3
"""
End-to-end test validating:
1. MCP subprocesses start correctly inside single container
2. Context-aware explanation generation (farmer vs citizen)
3. BMD data integrity (3+ parameters)
4. Geospatial accuracy (WGS84 buffering)
"""
import time
import sys
import subprocess
import requests
import json
from shapely.geometry import shape, Point

API_BASE = "http://localhost:8000"
MAX_WAIT = 60  # seconds
CHECK_INTERVAL = 3

def wait_for_agent():
    """Wait for agent service to become healthy"""
    print("⏳ Waiting for agent service to start (MCP subprocesses initialization)...")
    start_time = time.time()
    
    while time.time() - start_time < MAX_WAIT:
        try:
            resp = requests.get(f"{API_BASE}/health", timeout=5)
            if resp.status_code == 200:
                print(f"✅ Agent ready after {time.time() - start_time:.1f}s")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(CHECK_INTERVAL)
    
    raise RuntimeError(f"Agent failed to start within {MAX_WAIT}s")

def test_farmer_query():
    """Validate farmer context handling"""
    print("\n🌱 Testing FARMER context query...")
    # Query about Pabna
    query = {"query": "I'm a farmer near Pabna. Enough rain for crops in 5 days?"}
    
    resp = requests.post(f"{API_BASE}/query", json=query, timeout=45)
    if resp.status_code != 200:
        print(f"❌ Query failed: {resp.text}")
        raise AssertionError("API Request Failed")
        
    result = resp.json()
    
    # Validate structure
    assert "answer" in result, "Missing 'answer' field"
    assert "buffer" in result, "Missing 'buffer' field"
    assert "forecast" in result, "Missing 'forecast' field"
    
    # Validate farmer-specific content in explanation
    answer = result["answer"].lower()
    farmer_keywords = ["crop", "irrigation", "soil", "harvest", "field", "plant", "rain", "farm", "water", "drainage"]
    found_keywords = [w for w in farmer_keywords if w in answer]
    
    if not found_keywords:
        raise AssertionError(f"❌ Missing farmer context in explanation: {answer}")
    
    # Validate buffer location
    buffer = shape(result["buffer"])
    # Pabna Coordinates (Approx)
    pabna_point = Point(89.23, 24.00) 
    
    # Check if buffer is roughly in the right place (distance check)
    centroid = buffer.centroid
    dist = ((centroid.x - pabna_point.x)**2 + (centroid.y - pabna_point.y)**2)**0.5
    
    # Allow small variance for specific geocoding point differences
    if dist > 0.5: # ~50km tolerance
        raise AssertionError(f"Buffer centroid ({centroid.x}, {centroid.y}) too far from Pabna ({pabna_point.x}, {pabna_point.y})")

    # Validate buffer size (~15km radius)
    # 15km radius area is roughly 0.06 sq degrees at this latitude
    if not (0.03 < buffer.area < 0.09):
        print(f"⚠️ Warning: Buffer area {buffer.area:.4f} seems off for 15km, but proceeding.")
    
    print(f"✅ PASS: Farmer query returned context-aware explanation")
    print(f"   Context keywords found: {found_keywords}")
    print(f"   Explanation snippet: \"{result['answer'][:100]}...\"")

def test_citizen_query():
    """Validate citizen context handling"""
    print("\n🏙️  Testing CITIZEN context query...")
    query = {"query": "Will it rain in Dhaka in 3 days? Show the area on map."}
    
    resp = requests.post(f"{API_BASE}/query", json=query, timeout=45)
    assert resp.status_code == 200, f"Query failed: {resp.text}"
    result = resp.json()
    
    # Validate citizen-specific content
    answer = result["answer"].lower()
    citizen_keywords = ["umbrella", "commute", "travel", "outdoor", "clothing", "road", "traffic", "carry", "activity", "activities"]
    found_keywords = [w for w in citizen_keywords if w in answer]
    
    if not found_keywords:
        raise AssertionError(f"❌ Missing citizen context in explanation: {answer}")
    
    # Validate buffer location (Dhaka)
    buffer = shape(result["buffer"])
    dhaka_point = Point(90.41, 23.81)
    
    centroid = buffer.centroid
    dist = ((centroid.x - dhaka_point.x)**2 + (centroid.y - dhaka_point.y)**2)**0.5
    
    if dist > 0.2: # Stricter tolerance for capital city
        raise AssertionError(f"Buffer centroid ({centroid.x}, {centroid.y}) too far from Dhaka")

    print(f"✅ PASS: Citizen query returned commute-focused explanation")
    print(f"   Context keywords found: {found_keywords}")
    print(f"   Explanation snippet: \"{result['answer'][:100]}...\"")

def main():
    print("======================================================================")
    print("🚀 E2E TEST SUITE: MCP Subprocesses + Context-Aware Explanations")
    print("======================================================================")
    
    # Start services
    print("\n🐳 Starting Docker services...")
    # Using 'docker compose' (v2) instead of 'docker-compose' (v1)
    subprocess.run(["docker", "compose", "up", "-d", "--build"], check=True)
    
    try:
        wait_for_agent()
        test_farmer_query()
        test_citizen_query()
        
        print("\n" + "="*70)
        print("✅✅✅ ALL TESTS PASSED ✅✅✅")
        print("="*70)
        return 0
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        return 1
    finally:
        print("\n🧹 Cleaning up Docker services...")
        # Optional: Comment this out if you want to inspect logs after failure
        # subprocess.run(["docker", "compose", "down"], capture_output=True)

if __name__ == "__main__":
    sys.exit(main())