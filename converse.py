#!/usr/bin/env python3
"""
Nina Calls — Conversation Loop (STT → Process → TTS → Respond)
Connects to nina-calls server and handles a voice conversation.
"""
import sys, os, time, json, struct, subprocess, urllib.request, io, wave

CALLS_HOST = os.environ.get("NINA_CALLS_HOST", "http://localhost:9095")
WHISPER_HOST = os.environ.get("WHISPER_HOST", "http://localhost:7890")
SID = os.environ.get("SID", "")
CALL_ID = os.environ.get("CALL_ID", "")
CHUNK_MS = 500  # audio chunk size in ms

def pcm_to_wav_bytes(pcm_data: bytes, rate: int = 16000) -> bytes:
    """Convert raw 16-bit mono PCM to WAV bytes."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()

def transcribe(pcm_data: bytes) -> str:
    """Send PCM to Whisper for transcription using multipart/form-data."""
    wav = pcm_to_wav_bytes(pcm_data)
    boundary = "----NinaCallsWhisper"
    body = b"--" + boundary.encode() + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += wav
    body += b"\r\n--" + boundary.encode() + b"--\r\n"
    
    req = urllib.request.Request(
        f"{WHISPER_HOST}/transcribe",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        return result.get("text", "").strip()
    except Exception as e:
        print(f"  STT error: {e}", file=sys.stderr)
        return ""

def text_to_pcm(text: str, rate: int = 16000) -> bytes:
    """Convert text to 16kHz mono int16 PCM using edge-tts."""
    if not text.strip():
        return b""
    try:
        # Use edge-tts to generate MP3, then convert to PCM
        mp3_path = "/tmp/nina-resp.mp3"
        pcm_path = "/tmp/nina-resp.pcm"
        
        result = subprocess.run(
            ["edge-tts", "--voice", "pt-BR-AntonioNeural", "--text", text,
             "--write-media", mp3_path],
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            print(f"  TTS error: {result.stderr.decode()[:200]}", file=sys.stderr)
            return b""
        
        # Convert MP3 to PCM
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-f", "s16le", "-acodec", "pcm_s16le",
             "-ar", str(rate), "-ac", "1", pcm_path],
            capture_output=True, timeout=10
        )
        if result.returncode != 0:
            print(f"  ffmpeg error: {result.stderr.decode()[:200]}", file=sys.stderr)
            return b""
        
        with open(pcm_path, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"  TTS exception: {e}", file=sys.stderr)
        return b""

def inject_pcm(pcm_data: bytes) -> bool:
    """Send PCM audio to the active call."""
    try:
        req = urllib.request.Request(
            f"{CALLS_HOST}/api/sessions/{SID}/calls/{CALL_ID}/pcm",
            data=pcm_data,
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 204
    except Exception as e:
        print(f"  Inject error: {e}", file=sys.stderr)
        return False

def get_peer_audio() -> bytes:
    """Get buffered peer audio from the call."""
    try:
        req = urllib.request.Request(
            f"{CALLS_HOST}/api/sessions/{SID}/calls/{CALL_ID}/pcm"
        )
        resp = urllib.request.urlopen(req, timeout=2)
        if resp.status == 200:
            return resp.read()
    except:
        pass
    return b""

def generate_response(transcript: str) -> str:
    """Simple response logic — can be replaced with LLM/Nina."""
    transcript = transcript.lower().strip()
    if not transcript:
        return ""
    if any(w in transcript for w in ["oi", "olá", "ola", "alô"]):
        return "Olá! Aqui é a Nina, assistente virtual da RMS. Como posso ajudar?"
    if any(w in transcript for w in ["cotação", "cotacao", "plano", "preço", "preco"]):
        return "Claro! Me diga qual operadora e quantos beneficiários para eu fazer a cotação."
    if any(w in transcript for w in ["obrigado", "obrigada", "valeu", "tchau"]):
        return "Por nada! Qualquer coisa é só chamar. Até mais!"
    return "Entendi. Pode me dar mais detalhes para eu te ajudar melhor?"

def main():
    global SID, CALL_ID
    
    if not SID or not CALL_ID:
        print("Usage: SID=<session_id> CALL_ID=<call_id> python3 converse.py")
        sys.exit(1)
    
    print(f"🎙️  Conversation loop started — call {CALL_ID}")
    print(f"   Whisper: {WHISPER_HOST}")
    print(f"   Nina Calls: {CALLS_HOST}")
    print(f"   Say 'tchau' or hang up to end.\n")
    
    silent_chunks = 0
    max_silent = 40  # ~20s silence = end call
    
    # Send greeting
    greeting = "Olá! Aqui é a Nina. Em que posso ajudar?"
    print(f"🤖 Nina: {greeting}")
    pcm = text_to_pcm(greeting)
    if pcm:
        inject_pcm(pcm)
        time.sleep(len(pcm) / 32000 + 0.5)  # wait for audio to play
    
    while True:
        audio = get_peer_audio()
        
        if len(audio) < 320:  # < 10ms — silence
            silent_chunks += 1
            if silent_chunks >= max_silent:
                print("🔇 Silêncio prolongado — encerrando.")
                break
            time.sleep(CHUNK_MS / 1000)
            continue
        
        silent_chunks = 0
        print(f"🎤 Peer: {len(audio)} bytes PCM → transcrevendo...")
        
        transcript = transcribe(audio)
        if not transcript:
            print("   (sem fala detectada)")
            continue
        
        print(f"👤 Peer disse: \"{transcript}\"")
        
        response = generate_response(transcript)
        if not response:
            continue
        
        print(f"🤖 Nina: \"{response}\"")
        pcm = text_to_pcm(response)
        if pcm:
            inject_pcm(pcm)
            time.sleep(len(pcm) / 32000 + 1.0)
    
    print("✅ Conversation ended.")

if __name__ == "__main__":
    main()
