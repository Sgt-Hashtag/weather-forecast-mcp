import React, { useState } from 'react';
import Map, { Source, Layer, GeolocateControl } from 'react-map-gl';
import mapboxgl from 'mapbox-gl';
import { submitQuery } from '../api.js';

import 'mapbox-gl/dist/mapbox-gl.css';

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;
// localhost
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const INITIAL_VIEW_STATE = {
    longitude: 90.4125,
    latitude: 23.8103,
    zoom: 7
};

export default function MapComponent() {
    const [viewState, setViewState] = useState(INITIAL_VIEW_STATE);
    const [query, setQuery] = useState('');
    const [response, setResponse] = useState(null);
    const [bufferGeojson, setBufferGeojson] = useState(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState(null);

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!query.trim()) return;

        setIsLoading(true);
        setError(null);
        setResponse(null);

       // Inside Map.jsx -> handleSubmit function

        try {
            const result = await submitQuery(query, API_URL);
            
            // Check for the correct nested structure from your agent.py return statement
            if (!result || !result.forecast || !result.forecast.forecast) {
                throw new Error("Invalid response format: Forecast data missing");
            }

            setResponse(result);
            
            if (result.buffer) {
                setBufferGeojson(result.buffer);

                // ✅ IMPORTANT: Ensure you handle the GeoJSON 'Polygon' type
                // The buffer returned by tool_buffer is usually a Feature or Polygon
                const coords = result.buffer.type === 'Feature' 
                    ? result.buffer.geometry.coordinates[0] 
                    : result.buffer.coordinates[0];

                const lons = coords.map(pt => pt[0]);
                const lats = coords.map(pt => pt[1]);
                const minLon = Math.min(...lons);
                const maxLon = Math.max(...lons);
                const minLat = Math.min(...lats);
                const maxLat = Math.max(...lats);

                setViewState({
                    longitude: (minLon + maxLon) / 2,
                    latitude: (minLat + maxLat) / 2,
                    zoom: 10,
                    transitionDuration: 1000
                });
            }
        } catch (err) {
            setError(err.message || 'Query failed');
            console.error('Query error:', err);
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', fontFamily: 'sans-serif' }}>
            {/* Query Panel */}
            <div style={{ 
                padding: '20px', 
                backgroundColor: '#fff', 
                boxShadow: '0 2px 10px rgba(0,0,0,0.1)',
                zIndex: 10,
                borderBottom: '1px solid #eee'
            }}>
                <form onSubmit={handleSubmit} style={{ display: 'flex', gap: '10px', maxWidth: '800px', margin: '0 auto' }}>
                    <input
                        type="text"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="e.g. 'Will it rain in Chittagong in 3 days?'"
                        style={{ 
                            flex: 1, 
                            padding: '12px 15px',
                            border: '1px solid #ddd',
                            borderRadius: '8px',
                            fontSize: '16px',
                            outline: 'none'
                        }}
                    />
                    <button 
                        type="submit"
                        disabled={isLoading}
                        style={{
                            padding: '10px 24px',
                            backgroundColor: isLoading ? '#ccc' : '#007bff',
                            color: 'white',
                            border: 'none',
                            borderRadius: '8px',
                            fontSize: '16px',
                            fontWeight: '600',
                            cursor: isLoading ? 'not-allowed' : 'pointer',
                            transition: 'background 0.2s'
                        }}
                    >
                        {isLoading ? 'Thinking...' : 'Ask AI'}
                    </button>
                </form>
                
                {error && (
                    <div style={{ 
                        color: '#d32f2f', 
                        marginTop: '15px', 
                        padding: '10px', 
                        backgroundColor: '#ffebee', 
                        borderRadius: '6px',
                        textAlign: 'center'
                    }}>
                        {error}
                    </div>
                )}
                
                {response && (
                    <div style={{ 
                        marginTop: '20px', 
                        padding: '20px', 
                        backgroundColor: '#f8f9fa',
                        borderRadius: '12px',
                        maxWidth: '800px',
                        margin: '20px auto 0'
                    }}>
                        <h3 style={{ margin: '0 0 10px 0', color: '#2c3e50' }}>
                            {response.forecast.location.area_name} Analysis
                        </h3>
                        <p style={{ margin: '0 0 20px 0', lineHeight: 1.6, fontSize: '16px', color: '#444' }}>
                            {response.answer}
                        </p>
                        
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '15px' }}>
                            {response.forecast.forecast && response.forecast.forecast.map((day, i) => (
                                <div key={i} style={{ 
                                    padding: '15px',
                                    backgroundColor: 'white',
                                    borderRadius: '8px',
                                    boxShadow: '0 2px 4px rgba(0,0,0,0.05)',
                                    border: '1px solid #e9ecef'
                                }}>
                                    <div style={{ fontWeight: 'bold', fontSize: '14px', color: '#007bff', marginBottom: '5px' }}>
                                        {day.date}
                                    </div>
                                    <div style={{ fontSize: '14px', marginBottom: '4px' }}>
                                        🌡️ {day.parameters.temperature.min}° - {day.parameters.temperature.max}°C
                                    </div>
                                    <div style={{ fontSize: '14px', marginBottom: '4px' }}>
                                        ☔ {day.parameters.precipitation.value}mm
                                    </div>
                                    <div style={{ fontSize: '14px', color: '#666' }}>
                                        💧 {day.parameters.humidity.value}% Hum
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </div>

            {/* Map */}
            <div style={{ flex: 1, position: 'relative' }}>
                <Map
                    {...viewState}
                    onMove={evt => setViewState(evt.viewState)}
                    mapStyle="mapbox://styles/mapbox/streets-v12"
                    mapboxAccessToken={MAPBOX_TOKEN}
                    style={{ width: '100%', height: '100%' }}
                >
                    <GeolocateControl position="top-right" />
                    
                    {bufferGeojson && (
                        <Source id="buffer-source" type="geojson" data={bufferGeojson}>
                            <Layer
                                id="buffer-fill"
                                type="fill"
                                paint={{
                                    'fill-color': '#007bff',
                                    'fill-opacity': 0.2
                                }}
                            />
                            <Layer
                                id="buffer-outline"
                                type="line"
                                paint={{
                                    'line-color': '#007bff',
                                    'line-width': 2,
                                    'line-dasharray': [2, 2]
                                }}
                            />
                        </Source>
                    )}
                </Map>
            </div>
            
            <div style={{ 
                padding: '8px', 
                backgroundColor: '#343a40', 
                color: '#fff',
                fontSize: '12px',
                textAlign: 'center'
            }}>
                WeatherWise AI • Bangladesh Meteorological Department Data • Powered by Mapbox MCP
            </div>
        </div>
    );
}