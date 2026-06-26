#!/usr/bin/env python3
"""
Nina Calls — Conversation Loop v3
- Fixed: accumulate peer audio (2s+) before Whisper STT
- Fixed: use `requests` for proper multipart upload
- Buffer fix in Go: 30s capture buffer (was 240ms)
"""
import sys, os, time, json, subprocess, io, wave
import requests

CALLS_HOST = os.environ.get("NINA_CALLS_HOST", "http://localhost:9095")
WHISPER_HOST = os.environ.get("WHISPER_HOST", "http://localhost:7890")
SID = os.environ.get("SID", "")
CALL_ID = os.environ.get("CALL_ID", "")
SAMPLE_RATE = 16000
MIN_SPEECH_BYTES = SAMPLE_RATE * 2 * 2  # 2 seconds of 16-bit mono
COLLECT_TIMEOUT = 3.0  # max seconds to accumulate before forcing transcription

def pcm_to_wav_bytes(pcm_data: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()

def transcribe(pcm_data: bytes) -> str:
    """Send PCM to Whisper via proper multipart upload."""
    if len(pcm_data) < MIN_SPEECH_BYTES:
        print(f"   (chunk muito curto: {len(pcm_data)} bytes = {len(pcm_data)/(SAMPLE_RATE*2):.2f}s)", file=sys.stderr)
        return ""
    wav = pcm_to_wav_bytes(pcm_data)
    try:
        resp = requests.post(
            f"{WHISPER_HOST}/transcribe",
            files={"file": ("audio.wav", wav, "audio/wav")},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "").strip()
            duration = data.get("duration", 0)
            if text:
                print(f"   STT: \"{text}\" ({duration:.1f}s)", file=sys.stderr)
            return text
        else:
            print(f"   STT HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return ""
    except Exception as e:
        print(f"   STT error: {e}", file=sys.stderr)
        return ""

def text_to_pcm(text: str, rate: int = SAMPLE_RATE) -> bytes:
    if not text.strip():
        return b""
    try:
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

def pcm_duration_sec(pcm_bytes: int) -> float:
    return pcm_bytes / (SAMPLE_RATE * 2)

def inject_pcm(pcm_data: bytes) -> bool:
    try:
        resp = requests.post(
            f"{CALLS_HOST}/api/sessions/{SID}/calls/{CALL_ID}/pcm",
            data=pcm_data,
            timeout=10
        )
        return resp.status_code == 204
    except Exception as e:
        print(f"  Inject error: {e}", file=sys.stderr)
        return False

def get_peer_audio(timeout_ms: int = 200) -> bytes:
    """Get one chunk of buffered peer audio (non-blocking if timeout_ms is small)."""
    try:
        url = f"{CALLS_HOST}/api/sessions/{SID}/calls/{CALL_ID}/pcm?timeout_ms={timeout_ms}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return resp.content
    except:
        pass
    return b""

def collect_peer_audio(min_duration: float = 2.0, max_wait: float = 4.0) -> bytes:
    """Accumulate peer audio chunks until we have min_duration seconds or max_wait elapsed.
    Returns accumulated raw PCM bytes."""
    all_audio = bytearray()
    deadline = time.time() + max_wait
    min_bytes = int(min_duration * SAMPLE_RATE * 2)
    
    while time.time() < deadline:
        chunk = get_peer_audio(timeout_ms=300)
        if chunk:
            all_audio.extend(chunk)
            current_dur = len(all_audio) / (SAMPLE_RATE * 2)
            if len(all_audio) >= min_bytes:
                break
        else:
            time.sleep(0.05)  # small sleep when no data
    
    return bytes(all_audio)

def generate_response(transcript: str) -> str:
    t = transcript.lower().strip()
    if not t:
        return ""
    if any(w in t for w in ["oi", "olá", "ola", "alô", "alo"]):
        return "Olá! Aqui é a Nina, assistente virtual da RMS. Como posso ajudar?"
    if any(w in t for w in ["cotação", "cotacao", "plano", "preço", "preco", "valor"]):
        return "Claro! Me diga qual operadora e quantos beneficiários para eu fazer a cotação."
    if any(w in t for w in ["obrigado", "obrigada", "valeu"]):
        return "Por nada! Qualquer coisa é só chamar. Até mais!"
    if any(w in t for w in ["tchau", "adeus", "até mais", "ate mais"]):
        return "Até mais! Foi um prazer ajudar."
    return "Entendi. Pode me dar mais detalhes para eu te ajudar melhor?"

def speak_and_listen(text: str) -> bytes:
    """Synthesize TTS → inject → collect peer audio during playback."""
    print(f"🤖 Nina: \"{text}\"")
    pcm = text_to_pcm(text)
    if not pcm:
        return b""
    
    dur = pcm_duration_sec(len(pcm))
    print(f"   🎵 {len(pcm)} bytes PCM ({dur:.1f}s) → injecting...")
    
    if not inject_pcm(pcm):
        print("   ❌ Inject FAILED!")
        return b""
    
    print(f"   ✅ Injected. Listening for {dur + 1:.1f}s...")
    peer = collect_peer_audio(min_duration=1.5, max_wait=dur + 1.5)
    
    if peer:
        print(f"   🎤 Collected {len(peer)} bytes ({pcm_duration_sec(len(peer)):.1f}s) during playback")
    
    return peer

def main():
    global SID, CALL_ID
    
    if not SID or not CALL_ID:
        print("Usage: SID=<session_id> CALL_ID=<call_id> python3 converse.py", file=sys.stderr)
        sys.exit(1)
    
    print(f"🎙️  Nina Calls v3 — Call {CALL_ID}", file=sys.stderr)
    print(f"   Whisper: {WHISPER_HOST}  |  Server: {CALLS_HOST}", file=sys.stderr)
    print(f"   Min speech: {MIN_SPEECH_BYTES/1024:.0f}KB ({MIN_SPEECH_BYTES/(SAMPLE_RATE*2):.1f}s)\n", file=sys.stderr)
    
    # Greeting
    greeting = "Olá! Aqui é a Nina. Em que posso ajudar?"
    peer_audio = speak_and_listen(greeting)
    
    # Process any interruption during greeting
    if peer_audio and len(peer_audio) >= MIN_SPEECH_BYTES:
        transcript = transcribe(peer_audio)
        if transcript:
            print(f"👤 Peer: \"{transcript}\"")
            resp = generate_response(transcript)
            if resp and not any(w in transcript.lower() for w in ["tchau", "adeus"]):
                speak_and_listen(resp)
    
    silence_count = 0
    max_silence = 5  # ~8s total
    
    while True:
        print("👂 Waiting for speech...", file=sys.stderr)
        audio = collect_peer_audio(min_duration=1.5, max_wait=3.0)
        
        if len(audio) < MIN_SPEECH_BYTES:
            silence_count += 1
            print(f"   silence #{silence_count} ({len(audio)} bytes)", file=sys.stderr)
            if silence_count >= max_silence:
                print("🔇 Silence timeout — ending.", file=sys.stderr)
                bye = "Até mais! Qualquer coisa é só chamar."
                pcm = text_to_pcm(bye)
                if pcm:
                    inject_pcm(pcm)
                    time.sleep(pcm_duration_sec(len(pcm)) + 1)
                break
            continue
        
        silence_count = 0
        dur = pcm_duration_sec(len(audio))
        print(f"🎤 {len(audio)} bytes ({dur:.1f}s) → transcribing...", file=sys.stderr)
        
        transcript = transcribe(audio)
        if not transcript:
            continue
        
        print(f"👤 Peer: \"{transcript}\"")
        
        # Check for goodbye
        if any(w in transcript.lower() for w in ["tchau", "adeus", "até mais", "ate mais"]):
            bye = "Até mais! Foi um prazer ajudar."
            speak_and_listen(bye)
            break
        
        resp = generate_response(transcript)
        if resp:
            speak_and_listen(resp)
    
    print("✅ Conversation ended.", file=sys.stderr)

if __name__ == "__main__":
    main()
