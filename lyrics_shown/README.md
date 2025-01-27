# Lyrics Display Detection Server

## Overview

This script is designed to detect whether lyrics or videos/graphics are being displayed on a projector connected to OBS (Open Broadcaster Software). It achieves this by analyzing motion and color changes between consecutive screenshots of the projector's scene.

## Features

- **Motion Detection**: Uses OpenCV to compare images and detect significant motion or scene changes. Non-grayscale pixels are weighted higher to emphasize color changes.
- **OBS Integration**: Connects to OBS via WebSocket to capture screenshots of the projector scene.
- **API Server**: Provides a simple Flask API endpoint to query the current state (`lyrics_shown`), indicating whether lyrics are being displayed or not.
- **Rolling Image Buffer**: Maintains a buffer of the last 5 images for comparison, ensuring efficient motion detection.
- **Customizable Sensitivity**: Parameters for motion detection can be adjusted, including the threshold for motion detection and the color multiplier.

## Prerequisites

- OBS with WebSocket enabled.
- Python 3.x with the following libraries installed:
  - `obsws_python`
  - `flask`
  - `opencv-python`
  - `numpy`
  - `Pillow`

## Configuration

The script expects a `config.json` file located in the parent directory with the following structure:

```json
{
  "obs": {
    "websocket": {
      "host": "localhost",
      "port": 4455,
      "password": "your_password"
    }
  },
  "scenes": {
    "projector_scene": "Scene Name"
  }
}
