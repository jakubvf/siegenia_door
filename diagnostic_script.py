#!/usr/bin/env python3
"""
Siegenia Door Diagnostic Script

This script connects to your Siegenia door and queries all available features/parameters.
Use this to troubleshoot compatibility issues and see what fields your door model supports.

Instructions:
1. Update the configuration variables below with your door's details
2. Run the script: python3 diagnostic_script.py
3. Check the output to see what fields are available on your door
"""

import websocket
import ssl
import json
import sys
from typing import Dict, Any

# =============================================================================
# CONFIGURATION - UPDATE THESE VALUES FOR YOUR DOOR
# =============================================================================
DOOR_HOST = "192.168.1.100"  # Replace with your door's IP address
USERNAME = "your_username"    # Replace with your door's username
PASSWORD = "your_password"    # Replace with your door's password

# =============================================================================
# DIAGNOSTIC FUNCTIONS
# =============================================================================

def connect_and_login(host: str, username: str, password: str) -> websocket.WebSocket:
    """Connect to the door and authenticate."""
    print(f"Connecting to door at {host}...")
    
    try:
        ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.connect(f"wss://{host}/WebSocket")
        
        login_command = {
            'command': 'login',
            'user': username,
            'password': password,
            'long_life': False,
            'id': 2,
        }
        
        print("Sending login command...")
        ws.send(json.dumps(login_command))
        response = ws.recv()
        response_data = json.loads(response)
        
        status = response_data.get('status')
        if status != 'ok':
            print(f"❌ Login failed. Response: {response_data}")
            sys.exit(1)
        
        print("✅ Login successful!")
        return ws
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

def query_device_info(ws: websocket.WebSocket) -> Dict[str, Any]:
    """Query basic device information."""
    print("\n" + "="*60)
    print("QUERYING DEVICE INFO (getDevice command)")
    print("="*60)
    
    try:
        ws.send('{"command":"getDevice", "id":3}')
        response = ws.recv()
        response_data = json.loads(response)
        
        print("Raw response:")
        print(json.dumps(response_data, indent=2))
        
        if response_data.get('status') == 'ok':
            print("\n✅ Device info query successful!")
            return response_data
        else:
            print(f"\n❌ Device info query failed: {response_data}")
            return {}
            
    except Exception as e:
        print(f"❌ Error querying device info: {e}")
        return {}

def query_device_params(ws: websocket.WebSocket) -> Dict[str, Any]:
    """Query device parameters/status."""
    print("\n" + "="*60)
    print("QUERYING DEVICE PARAMETERS (getDeviceParams command)")
    print("="*60)
    
    try:
        ws.send('{"command":"getDeviceParams", "id":3}')
        response = ws.recv()
        response_data = json.loads(response)
        
        print("Raw response:")
        print(json.dumps(response_data, indent=2))
        
        if response_data.get('status') == 'ok':
            print("\n✅ Device parameters query successful!")
            return response_data
        else:
            print(f"\n❌ Device parameters query failed: {response_data}")
            return {}
            
    except Exception as e:
        print(f"❌ Error querying device parameters: {e}")
        return {}

def analyze_compatibility(device_info: Dict[str, Any], device_params: Dict[str, Any]):
    """Analyze the responses and check for required fields."""
    print("\n" + "="*60)
    print("COMPATIBILITY ANALYSIS")
    print("="*60)
    
    # Check required fields for the Home Assistant integration
    required_device_fields = ['mac', 'serialnr', 'systemname', 'softwareversion', 'hardwareversion']
    required_param_fields = ['daymode', 'state']
    
    print("\n🔍 Checking device info fields:")
    device_data = device_info.get('data', {})
    for field in required_device_fields:
        if field in device_data:
            print(f"  ✅ {field}: {device_data[field]}")
        else:
            print(f"  ❌ {field}: MISSING")
    
    print("\n🔍 Checking device parameter fields:")
    param_data = device_params.get('data', {})
    for field in required_param_fields:
        if field in param_data:
            print(f"  ✅ {field}: {param_data[field]}")
        else:
            print(f"  ❌ {field}: MISSING (This may cause integration errors)")
    
    print("\n📋 All available device info fields:")
    for key, value in device_data.items():
        print(f"  • {key}: {value}")
    
    print("\n📋 All available device parameter fields:")
    for key, value in param_data.items():
        print(f"  • {key}: {value}")
    
    # Specific analysis for the reported error
    print("\n🚨 SPECIFIC ISSUE ANALYSIS:")
    if 'daymode' not in param_data:
        print("  ❌ The 'daymode' field is missing from device parameters.")
        print("     This is causing the KeyError in the Home Assistant integration.")
        print("     Your door model may not support day/night mode functionality.")
        print("     The integration code needs to be updated to handle this gracefully.")
    else:
        print("  ✅ The 'daymode' field is present. The error might be intermittent.")

def main():
    """Main diagnostic routine."""
    print("Siegenia Door Diagnostic Script")
    print("=" * 40)
    
    # Validate configuration
    if DOOR_HOST == "192.168.1.100" or USERNAME == "your_username" or PASSWORD == "your_password":
        print("❌ Please update the configuration variables at the top of this script!")
        print("   - DOOR_HOST: Your door's IP address")
        print("   - USERNAME: Your door's username")
        print("   - PASSWORD: Your door's password")
        sys.exit(1)
    
    # Connect and authenticate
    ws = connect_and_login(DOOR_HOST, USERNAME, PASSWORD)
    
    try:
        # Query device information
        device_info = query_device_info(ws)
        
        # Query device parameters
        device_params = query_device_params(ws)
        
        # Analyze compatibility
        analyze_compatibility(device_info, device_params)
        
        print("\n" + "="*60)
        print("DIAGNOSTIC COMPLETE")
        print("="*60)
        print("Please share the output above when reporting issues on GitHub.")
        print("This will help developers understand your door model's capabilities.")
        
    finally:
        ws.close()
        print("\n🔌 Connection closed.")

if __name__ == "__main__":
    main()