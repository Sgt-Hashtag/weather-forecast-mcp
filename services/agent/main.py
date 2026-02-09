import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agent import WeatherAgent
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Weather Forecast Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class QueryRequest(BaseModel):
    query: str

class QueryResponse(BaseModel):
    answer: str
    buffer: dict
    display_location: str
    forecast: dict

# Global agent instance (initialized on startup)
agent = None

@app.on_event("startup")
async def startup_event():
    global agent
    logger.info("Initializing Weather Agent with MCP subprocesses...")
    agent = WeatherAgent()
    try:
        await agent.initialize(api_key=os.getenv("GOOGLE_API_KEY"))
        logger.info("Weather Agent ready with Mapbox + Weather MCP servers")
    except Exception as e:
        logger.error(f"Failed to initialize agent: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    global agent
    if agent:
        await agent.shutdown()
        logger.info("MCP subprocesses terminated cleanly")

@app.get("/health")
async def health_check():
    if agent is None or not agent.initialized:
        raise HTTPException(status_code=503, detail="Agent not ready")
    return {"status": "healthy", "mcp_servers": ["mapbox", "weather"]}

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    if agent is None or not agent.initialized:
        raise HTTPException(status_code=503, detail="Agent not ready")
    
    try:
        logger.info(f"🔍 Processing query: '{request.query}'")
        result = await agent.process_query(request.query)
        logger.info("Query processed successfully")
        return result
    except Exception as e:
        logger.error(f"Query processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))