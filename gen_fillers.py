#!/usr/bin/env python3
"""Pre-generate filler audio clips for phone conversations."""
import asyncio, subprocess, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILLER_DIR = os.path.join(BASE_DIR, "phone_fillers")
os.makedirs(FILLER_DIR, exist_ok=True)

FILLERS = {
    "thinking": "请稍等，让我记录一下。",
    "timeout": "喂，您还在吗？",
    "ack": "好的，明白了。",
    "repeat": "不好意思，我没听清楚，您能再说一遍吗？",
    "bye": "好的，谢谢您，再见。",
}

async def gen_one(key, text):
    mp3 = os.path.join(FILLER_DIR, f"{key}.mp3")
    wav = os.path.join(FILLER_DIR, f"{key}.wav")
    proc = await asyncio.create_subprocess_exec(
        "edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
        "--text", text, "--write-media", mp3,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if proc.returncode != 0:
        print(f"FAIL {key}")
        return
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", mp3, "-ar", "8000", "-ac", "1",
        "-sample_fmt", "s16", wav,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    print(f"OK {key}: {text}")

async def main():
    for key, text in FILLERS.items():
        await gen_one(key, text)

asyncio.run(main())
