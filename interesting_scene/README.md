# Interesting Scene Server

## Overview

This script detects scenes with people in a live streaming setup and provides this data to the director. It leverages the YOLO v11 model for image classification and integrates with OBS Studio via its WebSocket API. The script also runs a Flask web server to expose the current status of detected scenes.

## Features

- **Person Detection**: Uses the YOLO v11 model to analyze screenshots of OBS scenes and identify if a person is present.
- **OBS Integration**: Connects to OBS Studio using its WebSocket API to fetch screenshots of the current and other configured scenes.
- **Scene Status Updates**: Maintains a list of scenes with detected people and the status of the current program scene.
- **Web Interface**: Provides a `/status` endpoint to access the detection results in real-time as JSON.

## How It Works

1. The script connects to OBS Studio via WebSocket and fetches screenshots of the current program scene and other pre-configured scenes.
2. Screenshots are processed using the YOLO v11 model to classify objects and determine if a person is present.
3. Detected scenes with people are stored in a list and exposed through a web server for easy access.
4. The detection process runs in a separate thread, ensuring continuous updates without blocking the Flask web server.

## Installation

### Prerequisites
- Python 3.9+
- OBS Studio with WebSocket plugin enabled
- YOLO v11 model (`yolo11n.pt`) downloaded and accessible
- A `config.json` file in the parent directory with the following structure:
  ```json
  {
    "obs": {
      "websocket": {
        "host": "localhost",
        "port": 4455,
        "password": "your_password"
      },
      "scenes": {
        "list": ["Scene 1", "Scene 2", "Scene 3"]
      }
    }
  }
