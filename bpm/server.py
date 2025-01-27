import sounddevice as sd
import numpy as np
import librosa
from collections import deque
from flask import Flask, jsonify
import threading

# Parameters
INPUT_DEVICE = 1
BUFFER_DURATION = 15  # seconds
SAMPLERATE = 44100
ROLLING_AVERAGE_WINDOW = 5  # Number of BPM values to average

# Rolling buffer for audio data
audio_buffer = deque(maxlen=int(SAMPLERATE * BUFFER_DURATION))

# Rolling buffer for BPM values
bpm_history = deque(maxlen=ROLLING_AVERAGE_WINDOW)

# Shared state for BPM values
current_bpm = None
rolling_avg_bpm = None

# Flask app setup
app = Flask(__name__)

@app.route("/bpm", methods=["GET"])
def get_bpm():
    """Return the current and rolling average BPM as JSON."""
    return jsonify({
        "current_bpm": current_bpm,
        "rolling_average_bpm": rolling_avg_bpm
    })

# Beat detection function
def detect_bpm(audio_data, samplerate):
    onset_env = librosa.onset.onset_strength(y=audio_data, sr=samplerate)
    tempo, _ = librosa.beat.beat_track(onset_envelope=onset_env, sr=samplerate)
    return tempo

# Audio callback function
def audio_callback(indata, frames, time, status):
    if status:
        print(status)
    # Append new audio data to the rolling buffer
    audio_buffer.extend(indata[:, 0])

# Function to process audio and update BPM values
def process_audio_stream():
    global current_bpm, rolling_avg_bpm
    with sd.InputStream(device=INPUT_DEVICE, channels=2, samplerate=SAMPLERATE, callback=audio_callback):
        print("Listening for beats...")
        while True:
            if len(audio_buffer) == SAMPLERATE * BUFFER_DURATION:
                # Process the last BUFFER_DURATION seconds of audio
                audio_data = np.array(audio_buffer)
                bpm = detect_bpm(audio_data, SAMPLERATE)
                bpm = int(bpm[0])  # Convert detected tempo to an integer
                
                # Add BPM to the history and calculate the rolling average
                bpm_history.append(bpm)
                avg_bpm = int(sum(bpm_history) / len(bpm_history))
                
                # Update shared state
                current_bpm = bpm
                rolling_avg_bpm = avg_bpm

# Start audio processing in a separate thread
audio_thread = threading.Thread(target=process_audio_stream, daemon=True)
audio_thread.start()

# Start the Flask web server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3962, debug=False)
