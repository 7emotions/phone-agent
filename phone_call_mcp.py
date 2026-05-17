#!/usr/bin/env python3
import subprocess, json, time, os, tempfile, asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

ADB = ["adb"]
BT_CARD = "bluez_card.F8_AB_82_92_08_76"
BT_SINK = "bluez_sink.F8_AB_82_92_08_76.headset_audio_gateway"

server = Server("phone-call")


def adb(cmd: str, timeout: int = 15) -> str:
    r = subprocess.run(ADB + ["shell", cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def ensure_hsp():
    subprocess.run(["pactl", "set-card-profile", BT_CARD, "headset_audio_gateway"], capture_output=True)


def clean_env():
    env = {}
    for k, v in os.environ.items():
        if 'proxy' not in k.lower() and 'PROXY' not in k:
            env[k] = v
    env['HF_HUB_OFFLINE'] = '1'
    env['TRANSFORMERS_OFFLINE'] = '1'
    env['no_proxy'] = '*'
    return env


async def tts_8khz(text: str) -> str:
    raw = tempfile.mktemp(suffix=".mp3")
    pcm = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
        "--text", text, "--write-media", raw,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if proc.returncode != 0:
        return ""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", raw, "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", pcm,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    os.remove(raw)
    return pcm if proc.returncode == 0 else ""


async def asr_16khz(wav_8khz: str) -> str:
    upsampled = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", wav_8khz, "-ar", "16000", upsampled,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    os.remove(wav_8khz)
    proc = await asyncio.create_subprocess_exec(
        "whisper", upsampled, "--model", "small", "--language", "zh",
        "--output_format", "txt", "--output_dir", os.path.dirname(upsampled),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=clean_env())
    await proc.wait()
    txt = upsampled.rsplit(".", 1)[0] + ".txt"
    result = open(txt).read().strip() if os.path.exists(txt) else ""
    os.remove(upsampled)
    if os.path.exists(txt):
        os.remove(txt)
    return result


@server.list_tools()
async def list_tools():
    return [
        Tool(name="phone_dial", description="Dial a number. If greeting provided, generates TTS before dialing and plays 1s after connect.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "string"},
                 "greeting": {"type": "string"}
             }, "required": ["number"]}),
        Tool(name="phone_hangup", description="End current call",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_check", description="Check call state (0=idle, 1=ringing, 2=active)",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_speak", description="Speak TTS to caller via Bluetooth HSP",
             inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
        Tool(name="phone_listen", description="Record caller voice with VAD (stops on silence), then transcribe",
             inputSchema={"type": "object", "properties": {
                 "max_sec": {"type": "integer", "default": 30},
                 "silence_sec": {"type": "number", "default": 0.8}
             }}),
        Tool(name="phone_filler", description="Play a pre-generated filler audio instantly",
             inputSchema={"type": "object", "properties": {
                 "type": {"type": "string", "enum": ["thinking", "timeout", "ack", "repeat", "bye"]}
             }, "required": ["type"]}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "phone_dial":
        ensure_hsp()
        number = arguments["number"]
        greeting = arguments.get("greeting", "")

        wav = None
        if greeting:
            wav = await tts_8khz(greeting)

        adb(f"am start -a android.intent.action.CALL -d tel:{number}")

        for _ in range(60):
            state = adb("dumpsys telephony.registry | grep mCallState", timeout=3)
            if "mCallState=2" in state:
                if wav:
                    await asyncio.sleep(1)
                    proc = await asyncio.create_subprocess_exec(
                        "paplay", wav, "--device=" + BT_SINK,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await proc.wait()
                    os.remove(wav)
                return [TextContent(type="text", text=f"connected {number}")]
            await asyncio.sleep(0.3)
        if wav:
            os.remove(wav)
        return [TextContent(type="text", text=f"dialing {number}, no answer")]

    elif name == "phone_hangup":
        adb("input keyevent KEYCODE_ENDCALL")
        return [TextContent(type="text", text="hung up")]

    elif name == "phone_check":
        out = adb("dumpsys telephony.registry | grep mCallState", timeout=5)
        for line in out.split("\n"):
            if "mCallState=" in line:
                s = line.split("=")[1].strip()
                return [TextContent(type="text", text={"0": "idle", "1": "ringing", "2": "active"}.get(s, s))]
        return [TextContent(type="text", text="unknown")]

    elif name == "phone_speak":
        ensure_hsp()
        wav = await tts_8khz(arguments["text"])
        if not wav:
            return [TextContent(type="text", text="TTS failed")]
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        os.remove(wav)
        return [TextContent(type="text", text="spoken")]

    elif name == "phone_listen":
        ensure_hsp()
        max_sec = arguments.get("max_sec", 30)
        silence_sec = arguments.get("silence_sec", 0.8)
        wav = tempfile.mktemp(suffix=".wav")
        proc = await asyncio.create_subprocess_exec(
            "python3", "/home/ubuntu/phone_listen_vad.py",
            "--max-sec", str(max_sec),
            "--silence-sec", str(silence_sec),
            "--out", wav,
            stdout=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(wav):
            return [TextContent(type="text", text="(silence)"])
        transcript = await asr_16khz(wav)
        return [TextContent(type="text", text=transcript or "(unrecognized)")]

    elif name == "phone_filler":
        ensure_hsp()
        ft = arguments["type"]
        wav = f"/home/ubuntu/phone_fillers/{ft}.wav"
        if not os.path.exists(wav):
            return [TextContent(type="text", text=f"filler {ft} not found")]
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return [TextContent(type="text", text=f"filler:{ft}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
