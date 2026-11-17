"""服务器上的后台处理：提取音频、语音转写。不调用大模型。

- 提取音频（FFmpeg）：原片上传完成后自动运行，生成三个文件
  - 原音 FLAC：无损，保留原采样率，给粗剪 skill 出剪点和试听版
  - 16k 单声道 WAV：给语音转写
  - 试听 MP3：64 kbps 单声道，在网页上直接听
- 语音转写（SenseVoice，sherpa-onnx）：配置了 ASR_MODEL_DIR 才运行，
  输出逐字时间戳 JSON 和带时间的文字稿。没配置就跳过，不影响流程。

任务记在 jobs 表里，由一个后台线程依次处理；服务器重启后，没跑完的任务会重新排队。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

from . import db

log = logging.getLogger("uvicorn.error")

DERIVED = {  # field -> (标签, 后缀)
    "audio_master": ("原音（无损 FLAC）", ".flac"),
    "audio_asr": ("转写用音频（16k WAV）", ".wav"),
    "audio_preview": ("试听音频（MP3）", ".mp3"),
    "transcript_json": ("逐字转写（JSON）", ".json"),
    "transcript_txt": ("转写文字稿", ".txt"),
}
JOB_LABEL = {"extract_audio": "提取音频", "transcribe": "语音转写"}
JOB_STATUS = {"queued": "排队中", "running": "处理中", "done": "完成", "failed": "失败", "skipped": "未运行"}


def enqueue(conn, episode_id: int, kind: str, file_id: int) -> None:
    conn.execute("INSERT INTO jobs(episode_id, kind, file_id, status, created_at) VALUES(?,?,?, 'queued', ?)",
                 (episode_id, kind, file_id, db.now()))


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        lines = (p.stderr or p.stdout or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"退出码 {p.returncode}")
    return p.stdout


def probe(path: Path) -> dict:
    out = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]))
    info = {"duration": round(float(out.get("format", {}).get("duration") or 0), 2)}
    for s in out.get("streams", []):
        if s.get("codec_type") == "video" and "width" not in info:
            info.update(width=s.get("width"), height=s.get("height"), fps=s.get("avg_frame_rate"), vcodec=s.get("codec_name"))
        if s.get("codec_type") == "audio" and "sample_rate" not in info:
            info.update(sample_rate=int(s.get("sample_rate") or 0), channels=s.get("channels"), acodec=s.get("codec_name"))
    if "sample_rate" not in info:
        raise RuntimeError("文件里没有音轨")
    return info


def _add_derived(conn, store, src, field: str, key: str, extra: dict | None = None) -> int:
    label, suffix = DERIVED[field]
    stem = Path(src["filename"]).stem
    size = store.path(key).stat().st_size
    cur = conn.execute(
        "INSERT INTO files(episode_id, stage_key, field, filename, stored_name, size, uploaded_by, created_at, kind, source_file_id, meta) "
        "VALUES(?,?,?,?,?,?,NULL,?, 'derived', ?, ?)",
        (src["episode_id"], src["stage_key"], field, f"{stem}{suffix}", key, size, db.now(), src["id"],
         json.dumps(extra or {}, ensure_ascii=False)))
    return cur.lastrowid


def extract_audio(conn, store, src) -> dict:
    # 先做完耗时的 FFmpeg，最后在一个短事务里写库，避免长时间占住数据库写锁
    path = store.path(src["stored_name"])
    info = probe(path)
    base = f"episodes/{src['episode_id']}/derived/{src['id']}"
    outs = {
        "audio_master": ["-vn", "-map", "0:a:0", "-c:a", "flac"],
        "audio_asr": ["-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"],
        "audio_preview": ["-vn", "-map", "0:a:0", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k"],
    }
    keys = {}
    for field, args in outs.items():
        key = base + DERIVED[field][1]
        dest = store.save_path(key)
        tmp = dest.with_name(dest.stem + ".tmp" + dest.suffix)
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), *args, str(tmp)])
        tmp.replace(dest)
        keys[field] = key
    conn.execute("UPDATE files SET meta=? WHERE id=?", (json.dumps(info, ensure_ascii=False), src["id"]))
    made = {field: _add_derived(conn, store, src, field, key) for field, key in keys.items()}
    enqueue(conn, src["episode_id"], "transcribe", made["audio_asr"])
    return {"probe": info, "files": made}


# ---------- 语音转写 ----------

_recognizer = None


def asr_model_dir() -> Path | None:
    d = os.environ.get("ASR_MODEL_DIR")
    return Path(d) if d and (Path(d) / "model.int8.onnx").exists() else None


def _get_recognizer():
    global _recognizer
    if _recognizer is None:
        import sherpa_onnx
        d = asr_model_dir()
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
            language="auto", use_itn=True, num_threads=int(os.environ.get("ASR_THREADS", "2")))
    return _recognizer


def frame_energy(samples) -> "np.ndarray":
    """每 10 ms 一帧的能量（dB）。samples 是 16k 单声道 int16。"""
    import numpy as np
    hop = 160
    n = len(samples) // hop
    frames = samples[: n * hop].reshape(n, hop).astype(np.float32) / 32768
    return 10 * np.log10(np.einsum("ij,ij->i", frames, frames) / hop + 1e-10)


def split_points(energy, min_s: float = 8, max_s: float = 18) -> list[int]:
    """按静音切块：每块 8–18 秒，切在这个区间里能量最低的 10ms 帧。返回切点（帧序号，1 帧 = 10 ms）。"""
    import numpy as np
    n = len(energy)
    cuts, start = [0], 0
    while n - start > max_s * 100:
        lo, hi = start + int(min_s * 100), start + int(max_s * 100)
        best = lo + int(np.argmin(energy[lo:hi]))
        cuts.append(best)
        start = best
    cuts.append(n)
    return cuts


def transcribe(conn, store, src) -> dict:
    """分块读 WAV，内存只和块大小有关：3 小时的音频也只占几十 MB（加上模型约 1 GB）。"""
    if not asr_model_dir():
        return {"skipped": "没有配置 ASR_MODEL_DIR，跳过转写"}
    import numpy as np
    path = store.path(src["stored_name"])
    with wave.open(str(path)) as w:
        sr, total = w.getframerate(), w.getnframes()
        if sr != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("转写音频应为 16k 单声道 16 位 WAV")
        parts, block = [], sr * 60
        for _ in range(0, total, block):  # 能量一分钟一分钟地算
            parts.append(frame_energy(np.frombuffer(w.readframes(block), dtype=np.int16)))
        energy = np.concatenate(parts) if parts else np.zeros(0)
        cuts = split_points(energy)
        rec = _get_recognizer()
        tokens, segments = [], []
        for a, b in zip(cuts, cuts[1:]):
            if b - a < 10:
                continue
            w.setpos(a * 160)
            audio = np.frombuffer(w.readframes((b - a) * 160), dtype=np.int16).astype(np.float32) / 32768
            s = rec.create_stream()
            s.accept_waveform(sr, audio)
            rec.decode_stream(s)
            off = a / 100
            text = s.result.text.strip()
            if text:
                segments.append({"start": round(off, 2), "end": round(b / 100, 2), "text": text})
            tokens += [{"t": round(off + t, 2), "tok": tok} for tok, t in zip(s.result.tokens, s.result.timestamps)]
    data = {"engine": "sensevoice-int8", "sample_rate": sr, "segments": segments, "tokens": tokens}
    base = f"episodes/{src['episode_id']}/derived/{src['source_file_id'] or src['id']}"
    jkey, tkey = base + ".transcript.json", base + ".transcript.txt"
    store.save_path(jkey).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    store.save_path(tkey).write_text("\n".join(f"[{_ts(x['start'])}] {x['text']}" for x in segments), encoding="utf-8")
    orig = conn.execute("SELECT * FROM files WHERE id=?", (src["source_file_id"],)).fetchone() or src
    ids = {f: _add_derived(conn, store, orig, f, k) for f, k in (("transcript_json", jkey), ("transcript_txt", tkey))}
    return {"segments": len(segments), "files": ids}


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------- 后台线程 ----------

HANDLERS = {"extract_audio": extract_audio, "transcribe": transcribe}


def run_one(store) -> bool:
    """处理一个排队任务。没有任务返回 False。"""
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not job:
            return False
        if conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=? AND status='queued'",
                        (db.now(), job["id"])).rowcount == 0:
            return True
    with db.connect() as conn:
        src = conn.execute("SELECT * FROM files WHERE id=?", (job["file_id"],)).fetchone()
        label = JOB_LABEL.get(job["kind"], job["kind"])
        try:
            if not src:
                raise RuntimeError("源文件不存在")
            result = HANDLERS[job["kind"]](conn, store, src)
            status = "skipped" if "skipped" in result else "done"
            conn.execute("UPDATE jobs SET status=?, result=?, finished_at=? WHERE id=?",
                         (status, json.dumps(result, ensure_ascii=False), db.now(), job["id"]))
            db.log(conn, job["episode_id"], None, label + ("跳过" if status == "skipped" else "完成"), src["filename"] if src else "")
        except Exception as e:  # noqa: BLE001 —— 失败要记下来给人看
            conn.rollback()
            conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?", (str(e)[:500], db.now(), job["id"]))
            db.log(conn, job["episode_id"], None, label + "失败", f"{src['filename'] if src else ''}：{e}")
            log.exception("job %s failed", job["id"])
    return True


MAX_ATTEMPTS = 3
UPLOAD_TTL_DAYS = int(os.environ.get("UPLOAD_TTL_DAYS", "7"))


def recover(conn) -> None:
    """服务器重启后：没跑完的任务重新排队；已经因为重启中断了 3 次的（多半是内存不够被杀掉）标为失败，避免反复崩溃。"""
    conn.execute("UPDATE jobs SET status='failed', error='处理中服务器重启了 3 次（可能内存不够），已停止重试', finished_at=? "
                 "WHERE status='running' AND attempts>=?", (db.now(), MAX_ATTEMPTS))
    conn.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'")


def sweep_uploads(store) -> int:
    """放弃超过 UPLOAD_TTL_DAYS 天没有进展的未完成上传，释放磁盘。"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=UPLOAD_TTL_DAYS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        stale = conn.execute("SELECT * FROM uploads WHERE status='uploading' AND updated_at<?", (cutoff,)).fetchall()
        for up in stale:
            conn.execute("UPDATE uploads SET status='aborted', updated_at=? WHERE id=?", (db.now(), up["id"]))
            conn.execute("DELETE FROM upload_parts WHERE upload_id=?", (up["id"],))
            store.abort(up["stored_name"])
            db.log(conn, up["episode_id"], None, "清理未完成上传", f"{up['filename']}（{UPLOAD_TTL_DAYS} 天没有进展）")
    return len(stale)


def start_worker(store, stop: threading.Event) -> threading.Thread:
    """只能在一个进程里启动（多进程部署时其余进程设 WORKER=0）。"""
    with db.connect() as conn:
        recover(conn)

    def loop():
        last_sweep = 0.0
        while not stop.is_set():
            try:
                if time.time() - last_sweep > 3600:
                    sweep_uploads(store)
                    last_sweep = time.time()
                if not run_one(store):
                    stop.wait(2)
            except Exception:  # noqa: BLE001
                log.exception("worker error")
                stop.wait(5)

    t = threading.Thread(target=loop, name="media-worker", daemon=True)
    t.start()
    return t
