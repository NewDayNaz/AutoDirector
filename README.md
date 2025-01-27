# OBS Director

## Overview

`OBS Director` is an automation script designed to enhance live video production workflows using OBS Studio. It dynamically switches scenes based on the current state of the input stream, prioritizing engagement and ensuring smooth transitions. The script integrates multiple micro-services to analyze stream data and make informed decisions about scene changes.

See [obs_director/README.md](obs_director/README.md) for more info.

# Interesting Scene Server

## Overview

This script detects scenes with people in a live streaming setup and provides this data to the director. It leverages the YOLO v11 model for image classification and integrates with OBS Studio via its WebSocket API. The script also runs a Flask web server to expose the current status of detected scenes.

See [interesting_scene/README.md](interesting_scene/README.md) for more info.


# Lyrics Display Detection Server

## Overview

This script is designed to detect whether lyrics or videos/graphics are being displayed on a projector connected to OBS (Open Broadcaster Software). It achieves this by analyzing motion and color changes between consecutive screenshots of the projector's scene.

See [lyrics_shown/README.md](lyrics_shown/README.md) for more info.

# BPM Server - Beat Detection and Tempo Estimation

## Overview

This script sets up a server that detects beats in real-time audio using the `librosa` library. The server provides an endpoint to fetch the current beats per minute (BPM) and a rolling average of the BPM over a set duration. It listens to audio from a specified input device and processes the audio stream to estimate the tempo of the music or sound being played.

See [bpm/README.md](bpm/README.md) for more info.
