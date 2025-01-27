# OBS Director

## Overview

`OBS Director` is an automation script designed to enhance live video production workflows using OBS Studio. It dynamically switches scenes based on the current state of the input stream, prioritizing engagement and ensuring smooth transitions. The script integrates multiple micro-services to analyze stream data and make informed decisions about scene changes.

## Features

- **Dynamic Scene Switching**
  - Automatically switches to a specified projector scene if videos (e.g., lyrics or static content) are displayed.
  - Queues up random scenes with detected people if no person is present in the current program feed.
  - Ensures the program feed does not switch off the projector scene until it detects static content again.

- **Beat-Synchronized Transitions**
  - Integrates with a BPM (beats per minute) API to align transitions with the music's rhythm.
  - Randomizes the timing of scene changes while respecting the beat for a seamless experience.

- **State-Aware Switching**
  - Continuously monitors input stream data for:
    - Presence of people in the feed.
    - Current status of lyrics or video projections.
    - Interest levels for scenes based on micro-service feedback.

- **Configurable Scenes**
  - Supports a list of pre-defined scenes, including a dedicated projector scene.
  - Randomized selection of scenes to maintain variety and engagement.

## Requirements

- **Dependencies:**
  - Python 3.8+
  - [obsws-python](https://github.com/obsproject/obs-websocket) (for OBS WebSocket communication)
  - `requests` (for API interaction)
  - OBS Studio with WebSocket support enabled (requires OBS WebSocket plugin)

- **Configuration:**
  A `config.json` file is required in the parent directory with the following structure:
  ```json
  {
    "obs": {
      "websocket": {
        "host": "localhost",
        "port": 4455,
        "password": "PASSWORD_GOES_HERE"
      },
      "scenes": {
        "list": ["Scene1", "Scene2", "Scene3"],
        "projector_scene": "ProjectorScene",
        "wait_min": 3,
        "wait_max": 8
      }
    },
    "scene_interest_api": {
      "url": "http://example.com/scene_interest",
      "poll_interval": 1
    },
    "bpm_api": {
      "url": "http://example.com/bpm",
      "poll_interval": 5
    },
    "lyrics_api": {
      "url": "http://example.com/lyrics",
      "poll_interval": 1
    }
  }
