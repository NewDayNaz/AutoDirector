# BPM Server - Beat Detection and Tempo Estimation

## Overview

This script sets up a server that detects beats in real-time audio using the `librosa` library. The server provides an endpoint to fetch the current beats per minute (BPM) and a rolling average of the BPM over a set duration. It listens to audio from a specified input device and processes the audio stream to estimate the tempo of the music or sound being played.

## Features

- **Real-Time BPM Detection:** Continuously processes audio data to estimate the BPM (tempo) of the audio stream.
- **Rolling Average BPM:** Calculates and returns a rolling average BPM based on a specified window, smoothing out fluctuations in the tempo.
- **Flask Web API:** Exposes a simple RESTful API that provides the current BPM and rolling average BPM in JSON format.

## Requirements

- `sounddevice` for capturing audio from the input device.
- `numpy` for handling audio data arrays.
- `librosa` for audio processing and beat detection.
- `Flask` for creating the web server and API.
- `threading` for handling audio processing in the background while the server is running.
