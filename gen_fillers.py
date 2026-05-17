#!/usr/bin/env python3
"""Pre-generate filler audio clips for phone conversations."""
import asyncio, subprocess

FILLERS = {
    "thinking": "请稍等，让我记录一下。",
    "timeout": "喂，您还在吗？",
    "ack": "好的，明白了。",
    "repeat": "不好意思，我没听清楚，您能再说一遍吗？",
    "bye": "好的，谢谢您，再见。",
}

async def gen_one(key, text):
    mp3 = f"/home/ubuntu/phone_fillers/{key}.mp3"
    wav = f"/home/ubuntu/phone_fillers/{key}.wav"
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
