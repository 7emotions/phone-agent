#!/usr/bin/env python3
"""Stream-based VAD listener: record until silence, then return audio."""

import sys, struct, os, argparse
import webrtcvad
import subprocess

FRAME_MS = 30
SAMPLE_RATE = 8000

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-sec", type=int, default=30)
    parser.add_argument("--silence-sec", type=float, default=1.5)
    parser.add_argument("--vad-mode", type=int, default=2, choices=[0,1,2,3])
    parser.add_argument("--device", default="bluez_source.F8_AB_82_92_08_76.headset_audio_gateway")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    vad = webrtcvad.Vad(args.vad_mode)
    frame_size = int(SAMPLE_RATE * FRAME_MS / 1000)
    silence_frames = int(args.silence_sec * 1000 / FRAME_MS)
    max_frames = int(args.max_sec * 1000 / FRAME_MS)

    proc = subprocess.Popen(
        ["parecord", "--raw", f"--device={args.device}",
         "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    frames = []
    speech_started = False
    silent_count = 0
    total = 0

    while total < max_frames:
        data = proc.stdout.read(frame_size * 2)
        if len(data) < frame_size * 2:
            break

        is_speech = vad.is_speech(data, SAMPLE_RATE)
        frames.append(data)
        total += 1

        if is_speech:
            speech_started = True
            silent_count = 0
        elif speech_started:
            silent_count += 1
            if silent_count >= silence_frames:
                break

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    if not speech_started:
        sys.stderr.write("no_speech\n")
        sys.exit(1)

    with open(args.out, "wb") as f:
        f.write(b"RIFF")
        data_size = len(frames) * frame_size * 2
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
                            SAMPLE_RATE * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        for frame in frames:
            f.write(frame)

    sys.stderr.write(f"recorded {total * FRAME_MS}ms, {len(frames)} frames\n")
    sys.exit(0)

if __name__ == "__main__":
    main()
