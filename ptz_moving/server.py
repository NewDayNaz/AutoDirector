import asyncio
from aiohttp import web  # Make sure aiohttp is installed

ptzCameraMoving = False

# --- Web Server Section ---

async def handle_ptz_post(request):
    """
    HTTP POST handler for the root URL. Sets the ptzCameraMoving flag to True,
    then schedules a reset after 5 seconds.
    """
    global ptzCameraMoving
    ptzCameraMoving = True
    print("Received POST request at '/': ptzCameraMoving set to True")
    # Schedule the flag to be reset after 5 seconds
    asyncio.create_task(reset_ptz_after_delay())
    return web.Response(text="PTZ Camera movement started")

async def handle_ptz_get(request):
    """
    HTTP GET handler for the root URL. Returns the current value of ptzCameraMoving.
    """
    global ptzCameraMoving
    # Return the current value as JSON
    return web.json_response({"moving": ptzCameraMoving})

async def reset_ptz_after_delay():
    """Waits 5 seconds and then resets ptzCameraMoving to False."""
    global ptzCameraMoving
    await asyncio.sleep(5)
    ptzCameraMoving = False
    print("5 seconds elapsed: ptzCameraMoving reset to False")

async def start_web_server():
    """Starts the aiohttp web server on port 16842."""
    app = web.Application()
    # Add POST route for triggering the PTZ camera movement.
    app.router.add_post('/', handle_ptz_post)
    # Add GET route for retrieving the current state of ptzCameraMoving.
    app.router.add_get('/', handle_ptz_get)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 16842)
    await site.start()
    print("Web server running on port 16842")
    # Keep the server running forever.
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(start_web_server())
