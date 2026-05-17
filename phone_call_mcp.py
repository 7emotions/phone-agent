#!/usr/bin/env python3
"""MCP server: phone-call with context isolation.

phone_ask() wraps speak+listen+ASR+LLM into one call.
The LLM runs with isolated context: sees only the question + single transcript.
Parent agent receives structured JSON, never raw caller text.
"""

import subprocess, json, time, os, tempfile, asyncio, urllib.request
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ADB_BIN = os.environ.get("PHONE_ADB", os.path.expanduser("~/Android/Sdk/platform-tools/adb"))
ADB = [ADB_BIN]
BT_CARD = os.environ.get("PHONE_BT_CARD", "")
BT_SINK = os.environ.get("PHONE_BT_SINK", "")
LLM_URL = os.environ.get("PHONE_LLM_URL", "https://api.deepseek.com/v1/chat/completions")
LLM_KEY = os.environ.get("PHONE_LLM_KEY", "")
LLM_MODEL = os.environ.get("PHONE_LLM_MODEL", "deepseek-chat")

EXTRACT_PROMPT = """从对话文本中提取信息。输出严格 JSON。

需要的信息: {info_keys}

对话: {transcript}

{{"info": {{"字段": "值"}}, "done": true/false}}"""

server = Server("phone-call")


def adb(cmd: str, timeout: int = 15) -> str:
    r = subprocess.run(ADB + ["shell", cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def ensure_hsp() -> bool:
    r = subprocess.run(["pactl", "set-card-profile", BT_CARD, "headset_audio_gateway"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    # Verify the sink actually appeared
    r2 = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    return BT_SINK in r2.stdout
    if BT_SOURCE:
        subprocess.run(["pactl", "set-source-mute", BT_SOURCE, "1"],
                       capture_output=True)


def clean_env():
    env = {}
    for k, v in os.environ.items():
        if 'proxy' not in k.lower() and 'PROXY' not in k:
            env[k] = v
    env['HF_HUB_OFFLINE'] = '1'
    env['TRANSFORMERS_OFFLINE'] = '1'
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


def isolated_llm(info_keys: str, transcript: str) -> dict:
    """Single-turn LLM. Only sees info_keys + transcript. No system context leak."""
    prompt = EXTRACT_PROMPT.replace("{info_keys}", info_keys).replace("{transcript}", transcript)
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3, "max_tokens": 256
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_KEY}"
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception:
        return {"info": {}, "done": False}


async def record_vad(max_sec: int, silence_sec: float) -> str | None:
    wav = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
            "python3", os.path.join(BASE_DIR, "phone_listen_vad.py"),
        "--max-sec", str(max_sec), "--silence-sec", str(silence_sec),
        "--out", wav, stdout=asyncio.subprocess.DEVNULL)
    await proc.wait()
    return wav if proc.returncode == 0 and os.path.exists(wav) else None


@server.list_tools()
async def list_tools():
    return [
        Tool(name="phone_dial", description="Dial a phone number",
             inputSchema={"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]}),
        Tool(name="phone_hangup", description="End current call",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_check", description="Check call state",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_speak", description="TTS to caller via HSP",
             inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
        Tool(name="phone_ask", description="Ask caller a question, record, transcribe, extract structured info. Isolated LLM context.",
             inputSchema={"type": "object", "properties": {
                 "question": {"type": "string"},
                 "info_keys": {"type": "string", "description": "Comma-separated info fields, e.g. 出席,饮食"}
             }, "required": ["question", "info_keys"]}),
        Tool(name="phone_filler", description="Play pre-generated filler audio",
             inputSchema={"type": "object", "properties": {
                 "type": {"type": "string", "enum": ["thinking", "timeout", "ack", "repeat", "bye"]}
             }, "required": ["type"]}),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict):
    if name == "phone_dial":
        number = args["number"]
        adb(f"am start -a android.intent.action.CALL -d tel:{number}")
        for _ in range(20):
            state = adb("dumpsys telephony.registry | grep mCallState")
            if "mCallState=2" in state:
                return [TextContent(type="text", text=f"connected {number}")]
            await asyncio.sleep(1)
        return [TextContent(type="text", text=f"dialing {number}...")]

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
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        wav = await tts_8khz(args["text"])
        if not wav:
            return [TextContent(type="text", text="TTS failed")]
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        os.remove(wav)
        return [TextContent(type="text", text="spoken")]

    elif name == "phone_ask":
        if not ensure_hsp():
            return [TextContent(type="text", text=json.dumps({"info": {}, "done": False, "status": "bluetooth_disconnected"}, ensure_ascii=False))]
        question = args["question"]
        info_keys = args["info_keys"]

        await call_tool("phone_speak", {"text": question})
        await call_tool("phone_filler", {"type": "thinking"})

        await asyncio.sleep(0.5)
        wav = await record_vad(20, 0.8)
        if not wav:
            return [TextContent(type="text", text=json.dumps({"info": {}, "done": False, "status": "no_speech"}, ensure_ascii=False))]

        transcript = await asr_16khz(wav)
        result = isolated_llm(info_keys, transcript)
        result["status"] = "ok"
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    elif name == "phone_filler":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        ft = args["type"]
        wav = os.path.join(BASE_DIR, "phone_fillers", f"{ft}.wav")
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
