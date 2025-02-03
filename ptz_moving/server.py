import asyncio
import json
from aiohttp import web
import websockets

# Load configuration
config = None
with open("../config.json", "r") as file:
    config = json.load(file)

# Global state for PTZ camera movement
ptzCameraMoving = False

# WebSocket client to update ptz_moving value
async def update_ptz_moving(value):
    async with websockets.connect(config["coordinator"]["client"]) as websocket:
        message = json.dumps({"action": "update", "topic": "ptz_moving", "data": value})
        await websocket.send(message)
        print(f"Updated ptz_moving to {value} on WebSocket server")

# --- Web Server Section ---

async def handle_ptz_post(request):
    """
    HTTP POST handler for the root URL. Sets the ptzCameraMoving flag to True,
    then schedules a reset after 5 seconds.
    """
    global ptzCameraMoving
    ptzCameraMoving = True
    
    # Notify WebSocket server of the update
    await update_ptz_moving(ptzCameraMoving)
    
    # Schedule the flag to be reset after 5 seconds
    asyncio.create_task(reset_ptz_after_delay())
    return web.Response(text="PTZ Camera movement started")

async def reset_ptz_after_delay():
    """Waits 5 seconds and then resets ptzCameraMoving to False."""
    global ptzCameraMoving
    await asyncio.sleep(5)
    ptzCameraMoving = False
    
    # Notify WebSocket server of the update
    await update_ptz_moving(ptzCameraMoving)

async def start_web_server():
    """Starts the aiohttp web server on port 16842."""
    app = web.Application()
    # Add POST route for triggering the PTZ camera movement.
    app.router.add_post('/', handle_ptz_post)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 16842)
    await site.start()
    print("Web server running on port 16842")
    # Keep the server running forever.
    while True:
        await asyncio.sleep(3600)

# Main entry point
async def main():
    # Start the web server
    await start_web_server()

# Run the script
if __name__ == "__main__":
    asyncio.run(main())
