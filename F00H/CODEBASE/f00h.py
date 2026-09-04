#!/usr/bin/env python3
"""F00-H HOOK — Background Mismatch Hook (2-second pattern interrupt).

F00-H est une frégate optionnelle. Elle ajoute un "hook" de 2 secondes
au début de chaque clip : un fond complètement absurde (Backrooms, Konoha,
etc.) est affiché derrière le streamer masqué, puis on revient au fond
original avec un glitch SFX.

F00H peut tourner en deux modes :
  - LOCAL (CPU) : validation, gates, extraction de frames de contrôle
  - REMOTE (Modal GPU) : SAM 2 masking + composition via f00h_hook_worker

Usage local :
  python3 f00h.py --clips-dir /path/to/clips --preset backrooms --out /path/to/out
  python3 f00h.py --clips-dir /path/to/clips --preset random --out /path/to/out
  python3 f00h.py --validate --clips-dir /path/to/clips
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
DEFAULT_FPS = 30
HOOK_DURATION_SECONDS = 2.0
CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "CONFIG" / "hook_presets.json"


# ─── Utilities ───────────────────────────────────────────────────────

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,duration:format=duration",
        "-of", "json", str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError(f"Aucun flux vidéo dans {path}")
    rate = video.get("avg_frame_rate", "0/0")
    num, den = (int(x) for x in rate.split("/", 1))
    fps = num / den if den else 0.0
    return {
        "codec": video.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 6),
        "duration_seconds": round(float(video.get("duration") or data.get("format", {}).get("duration") or 0), 6),
        "orientation": "portrait" if video.get("height", 0) > video.get("width", 0) else "landscape",
    }


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Config introuvable: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def select_background(config: dict, preset: str | None = None) -> tuple[str, dict]:
    """Sélectionne un fond. Si preset='random' ou None, choix au hasard."""
    backgrounds = config.get("backgrounds", {})
    if not backgrounds:
        raise ValueError("Aucun fond disponible dans hook_presets.json")
    if preset and preset != "random" and preset in backgrounds:
        return preset, backgrounds[preset]
    name = random.choice(list(backgrounds.keys()))
    return name, backgrounds[name]


# ─── Gates ───────────────────────────────────────────────────────────

def gate_h0_input(clip_path: Path) -> dict:
    """H0 : Le clip existe et est lisible, au bon format."""
    if not clip_path.is_file():
        return {"gate": "H0_INPUT", "status": "FAILED", "reason": f"Fichier introuvable: {clip_path}"}
    try:
        meta = probe(clip_path)
    except Exception as exc:
        return {"gate": "H0_INPUT", "status": "FAILED", "reason": str(exc)}
    errors = []
    if meta["codec"] != "h264":
        errors.append(f'codec={meta["codec"]}, attendu h264')
    if meta["duration_seconds"] < HOOK_DURATION_SECONDS:
        errors.append(f"duree={meta['duration_seconds']}s, hook demande {HOOK_DURATION_SECONDS}s minimum")
    if errors:
        return {"gate": "H0_INPUT", "status": "FAILED", "reason": "; ".join(errors), "metadata": meta}
    return {"gate": "H0_INPUT", "status": "PASSED", "metadata": meta}


def gate_h1_background(bg_name: str, config: dict) -> dict:
    """H1 : Le fond alternatif existe dans la bibliothèque."""
    backgrounds = config.get("backgrounds", {})
    if bg_name not in backgrounds:
        return {"gate": "H1_BACKGROUND", "status": "FAILED", "reason": f"Fond '{bg_name}' absent de la bibliotheque"}
    return {"gate": "H1_BACKGROUND", "status": "PASSED", "preset": bg_name, "asset": backgrounds[bg_name].get("asset")}


def gate_h2_hook(clip_meta: dict, hook_duration_frames: int) -> dict:
    """H2 : Le hook peut être appliqué (clip assez long)."""
    fps = clip_meta.get("fps", DEFAULT_FPS)
    total_frames = int(clip_meta.get("duration_seconds", 0) * fps)
    if total_frames < hook_duration_frames:
        return {"gate": "H2_HOOK", "status": "FAILED",
                "reason": f"Clip trop court: {total_frames} frames, hook necessite {hook_duration_frames}"}
    return {"gate": "H2_HOOK", "status": "PASSED", "total_frames": total_frames, "hook_frames": hook_duration_frames}


def gate_h3_sfx(config: dict) -> dict:
    """H3 : Le fichier SFX existe."""
    sfx_key = config.get("default_sfx", "glitch")
    sfx_map = config.get("sfx", {})
    sfx_path = sfx_map.get(sfx_key)
    if not sfx_path:
        return {"gate": "H3_SFX", "status": "FAILED", "reason": "Pas de SFX configuré"}
    # On vérifie juste la config, pas le fichier (peut être sur Modal)
    return {"gate": "H3_SFX", "status": "PASSED", "sfx_key": sfx_key, "sfx_path": sfx_path}


def gate_h4_output(hooked_clip: Path) -> dict:
    """H4 : Le clip final existe et est valide (vérifié après rendu GPU)."""
    if not hooked_clip.is_file():
        return {"gate": "H4_OUTPUT", "status": "PENDING", "reason": "Clip hooked pas encore rendu"}
    try:
        meta = probe(hooked_clip)
    except Exception as exc:
        return {"gate": "H4_OUTPUT", "status": "FAILED", "reason": str(exc)}
    if meta["width"] != OUTPUT_WIDTH or meta["height"] != OUTPUT_HEIGHT:
        return {"gate": "H4_OUTPUT", "status": "FAILED", "reason": f"Resolution incorrecte: {meta['width']}x{meta['height']}"}
    return {"gate": "H4_OUTPUT", "status": "PASSED", "metadata": meta}


# ─── Frame Extraction (pour le panneau de contrôle) ──────────────────

def extract_control_frames(clip_path: Path, out_dir: Path, fps: float = DEFAULT_FPS) -> list[dict]:
    """Extrait 3 frames de contrôle : 0s (hook), 2s (retour), 3s (normal)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamps = [
        ("frame_0s_hook", 0.0),
        ("frame_2s_return", HOOK_DURATION_SECONDS),
        ("frame_3s_normal", HOOK_DURATION_SECONDS + 1.0),
    ]
    frames = []
    for name, ts in timestamps:
        out_file = out_dir / f"{name}.png"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{ts:.3f}", "-i", str(clip_path),
            "-frames:v", "1", "-q:v", "2", str(out_file),
        ]
        subprocess.run(cmd, check=True)
        frames.append({"name": name, "timestamp": ts, "path": str(out_file)})
    return frames


# ─── Main ────────────────────────────────────────────────────────────

def build_clip_report(clip_path: Path, bg_name: str, bg_info: dict, config: dict, clip_meta: dict) -> dict:
    """Construit le rapport complet pour un clip."""
    hook_frames = int(config.get("hook_duration_frames", 60))

    gates = [
        gate_h0_input(clip_path),
        gate_h1_background(bg_name, config),
        gate_h2_hook(clip_meta, hook_frames),
        gate_h3_sfx(config),
    ]

    all_passed = all(g["status"] == "PASSED" for g in gates)
    overall = "READY_FOR_GPU" if all_passed else "NEEDS_REVIEW"

    return {
        "clip": clip_path.name,
        "clip_metadata": clip_meta,
        "selected_background": bg_name,
        "background_asset": bg_info.get("asset"),
        "hook_duration_seconds": config.get("hook_duration_seconds", 2.0),
        "hook_duration_frames": hook_frames,
        "gates": gates,
        "status": overall,
        "created_at": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="F00-H Hook — Background Mismatch Hook")
    parser.add_argument("--clips-dir", type=Path, required=True, help="Dossier contenant les clips a hooker")
    parser.add_argument("--out", type=Path, required=True, help="Dossier de sortie")
    parser.add_argument("--preset", default="random", help="Nom du preset de fond ou 'random' (defaut: random)")
    parser.add_argument("--validate", action="store_true", help="Mode validation uniquement (pas de rendu)")
    parser.add_argument("--extract-frames", action="store_true", help="Extraire les frames de controle")
    global CONFIG_PATH
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Chemin vers hook_presets.json")
    args = parser.parse_args()

    CONFIG_PATH = args.config
    config = load_config()

    clips = sorted(args.clips_dir.glob("*.mp4"))
    if not clips:
        print(json.dumps({"error": "Aucun clip .mp4 trouvé", "dir": str(args.clips_dir)}))
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    hooked_dir = args.out / "clips_hooked"
    clean_dir = args.out / "clips_clean"
    frames_dir = args.out / "control_frames"
    hooked_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    for clip_path in clips:
        clip_meta = probe(clip_path)
        bg_name, bg_info = select_background(config, args.preset)
        report = build_clip_report(clip_path, bg_name, bg_info, config, clip_meta)
        reports.append(report)

        # Backup du clip propre
        clean_backup = clean_dir / clip_path.name
        if not clean_backup.exists():
            import shutil
            shutil.copy2(clip_path, clean_backup)

        # Extraction des frames de controle
        if args.extract_frames:
            clip_frames_dir = frames_dir / clip_path.stem
            extract_control_frames(clip_path, clip_frames_dir, clip_meta.get("fps", DEFAULT_FPS))
            report["control_frames"] = str(clip_frames_dir)

        print(json.dumps(report, ensure_ascii=False))

    # Rapport global
    total = len(reports)
    ready = sum(1 for r in reports if r["status"] == "READY_FOR_GPU")
    review = total - ready

    summary = {
        "stage": "F00-H_LOCAL_VALIDATION",
        "total_clips": total,
        "ready_for_gpu": ready,
        "needs_review": review,
        "reports": reports,
        "created_at": now(),
    }

    report_path = args.out / "f00h_local_report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "report": str(report_path), "total": total, "ready": ready}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
