import pyaudio
import numpy as np
from scipy.fft import fft
import time

# Parameters
RATE = 44100  # Sample rate (samples per second)
CHANNELS = 1  # Mono
FORMAT = pyaudio.paInt16  # 16-bit depth
CHUNK_SIZE = 1024  # Number of frames per buffer
BUFFER_SIZE = 5  # Number of buffers to keep in history
THRESHOLD = 1000000  # Minimum sum of frequency magnitudes to consider as music

# Global variable to store the result
music_playing = None
history = []

# Function to detect music by analyzing frequencies
def is_music(data):
    # Convert data to numpy array
    audio_data = np.frombuffer(data, dtype=np.int16)
    
    # Perform FFT to get frequency components
    freqs = fft(audio_data)
    magnitudes = np.abs(freqs[:len(freqs)//2])  # Only take the positive half of the spectrum

    # Sum the magnitudes of the frequencies
    frequency_sum = np.sum(magnitudes)
    print(frequency_sum)
    
    # Return True if music is detected (frequency sum exceeds threshold), otherwise False
    return frequency_sum > THRESHOLD

# Function to update the global music_playing variable based on the history
def update_music_status():
    global music_playing
    if len(history) == BUFFER_SIZE:
        if all(value == history[0] for value in history):  # Check if all last 5 values are the same
            last_music_playing = music_playing
            music_playing = history[0]
            
            if last_music_playing != music_playing:
              print(f"Music status updated: {'Music detected' if music_playing else 'No music detected'}")
        # else:
        #     print("No change in music status.")
    else:
        print("Not enough history to update status.")

# Initialize PyAudio
p = pyaudio.PyAudio()

# Open stream
stream = p.open(format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE)

print("Listening for music...")

# Main loop
try:
    while True:
        # Record audio chunk
        data = stream.read(CHUNK_SIZE)
        
        # Check if music is playing
        music_status = is_music(data)
        history.append(music_status)
        
        # Keep only the last X values in history
        if len(history) > BUFFER_SIZE:
            history.pop(0)
        
        # Update music status if necessary
        update_music_status()
        
        # Sleep for a bit to avoid overloading the console with output
        time.sleep(0.5)
except KeyboardInterrupt:
    print("Exiting...")

finally:
    # Close the stream
    stream.stop_stream()
    stream.close()
    p.terminate()
