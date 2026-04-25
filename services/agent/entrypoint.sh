#!/bin/bash
set -e

CREDENTIALS_FILE="/app/secrets/credentials.json"
TOKEN_FILE="/app/secrets/ee_token.json"
AUTH_DONE_FILE="/app/.gee_auth_done"

echo "=========================================="
echo "  GEE Authentication Setup"
echo "=========================================="

if [ -f "$CREDENTIALS_FILE" ]; then
    echo "[1/2] Service account credentials found at $CREDENTIALS_FILE"
    echo "[2/2] GEE will authenticate using service account..."
    echo ""
    echo "✅ GEE authentication will be handled by earthengine-api"
    echo "=========================================="
    touch "$AUTH_DONE_FILE"
elif [ -f "$TOKEN_FILE" ]; then
    echo "[1/2] GEE token file found at $TOKEN_FILE"
    echo "[2/2] Copying token to application credentials directory..."
    mkdir -p /root/.config/earthengine
    cp "$TOKEN_FILE" /root/.config/earthengine/credentials
    echo "✅ Token installed. GEE will authenticate using saved token."
    echo "=========================================="
    touch "$AUTH_DONE_FILE"
elif [ -f "$AUTH_DONE_FILE" ]; then
    echo "✅ GEE auth already completed (credentials exist)"
else
    echo "[1/3] No GEE credentials found."
    echo ""
    echo "You have two options for GEE authentication:"
    echo ""
    echo "  OPTION A: Service Account (Recommended for Docker)"
    echo "    - Create a service account in Google Cloud Console"
    echo "    - Download the JSON key file"
    echo "    - Place it at: ./secrets/credentials.json"
    echo ""
    echo "  OPTION B: Local Token (Authenticate on host, copy token)"
    echo "    - Authenticate locally: pip install earthengine-api && ee authenticate"
    echo "    - Run: earthengine authenticate --token_file=ee_token.json"
    echo "    - Place the token at: ./secrets/ee_token.json"
    echo ""
    read -p "Choose authentication method (A/B) [A]: " auth_choice
    auth_choice=${auth_choice:-A}

    if [ "$auth_choice" = "B" ] || [ "$auth_choice" = "b" ]; then
        echo ""
        echo "=========================================="
        echo "  Token-Based Authentication"
        echo "=========================================="
        echo ""
        echo "On your LOCAL machine (not in Docker), run:"
        echo ""
        echo "  1. pip install earthengine-api"
        echo "  2. earthengine authenticate --token_file=ee_token.json"
        echo "  3. Copy ee_token.json to ./secrets/"
        echo ""
        echo "Press ENTER when you've added ee_token.json..."
        read -r dummy
        
        if [ -f "$TOKEN_FILE" ]; then
            mkdir -p /root/.config/earthengine
            cp "$TOKEN_FILE" /root/.config/earthengine/credentials
            echo "✅ Token installed."
        else
            echo "❌ Token file not found. Using simulated fields."
        fi
        echo "=========================================="
    else
        echo ""
        echo "=========================================="
        echo "  Service Account Setup Required"
        echo "=========================================="
        echo ""
        echo "To enable GEE satellite-based field delineation:"
        echo ""
        echo "  1. Go to: https://console.cloud.google.com/apis/credentials"
        echo "  2. Create a service account or use an existing one"
        echo "  3. Grant 'Earth Engine' API access"
        echo "  4. Create a key (JSON) and download it"
        echo "  5. Place the JSON file as: ./secrets/credentials.json"
        echo "  6. Restart the container"
        echo ""
        echo "Press ENTER to continue with simulated fields..."
        read -r dummy
        echo ""
        echo "✅ Setup mode complete. Container will start with simulated fields."
        echo "   Add credentials and restart to enable GEE."
        echo "=========================================="
    fi
    touch "$AUTH_DONE_FILE"
fi

echo ""
echo "Starting application..."
exec "$@"