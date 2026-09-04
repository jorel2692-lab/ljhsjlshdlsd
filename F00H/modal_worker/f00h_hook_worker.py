"""Worker Modal GPU pour F00-H HOOK - SAM 2 masking + background swap."""
from __future__ import annotations
import json, os, subprocess, time
from pathlib import Path
import modal

APP_NAME = os.getenv("LACRIMAE_MODAL_APP", "lacrimae-dev10-video")
VIDEO_DIR = "/data"
MODEL_DIR = "/models"
SAM2_MODEL_DIR = Path(MODEL_DIR) / "models" / "SAM2"
CONFIG_PATH = Path("/app/CONFIG/hook_presets.json")
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0", "git")
    .pip_install(
        "torch", "torchvision",
        index_url="https://download.pytorch.org/whl/cu121",
    )
    .pip_install(
        "opencv-python-headless", "numpy", "gfpgan", "modal",
    )
    .pip_install("git+https://github.com/facebookresearch/sam2.git")
)

app = modal.App(APP_NAME)
video_volume = modal.Volume.from_name(os.getenv("LACRIMAE_VIDEO_VOLUME", "lacrimae-dev10-video"), create_if_missing=True)
model_volume = modal.Volume.from_name(os.getenv("LACRIMAE_MODEL_VOLUME", "lacrimae-dev10-models"), create_if_missing=True)

def _load_hook_config():
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"hook_duration_seconds": 2.0, "hook_duration_frames": 60, "backgrounds": {},
            "sfx": {"glitch": "sfx/glitch.mp3"}, "default_sfx": "glitch",
            "composition": {"transition_frame": 60, "sfx_volume": 0.8}}

def _load_sam2_model():
    import torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    model_cfg = str(SAM2_MODEL_DIR / "sam2_hiera_l.yaml")
    checkpoint = str(SAM2_MODEL_DIR / "sam2_hiera_large.pt")
    if not Path(model_cfg).is_file() or not Path(checkpoint).is_file():
        raise FileNotFoundError(f"SAM 2 model absents: {model_cfg}, {checkpoint}")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    sam2_model = build_sam2(model_cfg, checkpoint, device=str(device))
    return SAM2ImagePredictor(sam2_model), device

def _segment_streamer(predictor, frame_rgb, device):
    import numpy as np
    h, w = frame_rgb.shape[:2]
    input_points = np.array([[w // 2, h // 2]], dtype=np.float32)
    input_labels = np.array([1], dtype=np.int32)
    predictor.set_image(frame_rgb)
    masks, scores, _ = predictor.predict(point_coords=input_points, point_labels=input_labels, multimask_output=True)
    best_idx = int(np.argmax(scores))
    return masks[best_idx]

def _composite_hook_frame(original_frame, hook_bg_frame, mask, width, height):
    import cv2, numpy as np
    if hook_bg_frame.shape[:2] != (height, width):
        hook_bg_frame = cv2.resize(hook_bg_frame, (width, height))
    mask_3ch = np.stack([mask.astype(np.uint8)] * 3, axis=-1)
    composited = hook_bg_frame * (1 - mask_3ch) + original_frame * mask_3ch
    return composited.astype(np.uint8)

def _cover_resize(frame_bgr, target_w, target_h):
    """Met une frame en 'cover' (scale + crop centre) dans le format cible."""
    import cv2
    h, w = frame_bgr.shape[:2]
    scale = max(target_w / w, target_h / h)
    rw, rh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame_bgr, (rw, rh), interpolation=cv2.INTER_LANCZOS4)
    x0 = max(0, (rw - target_w) // 2)
    y0 = max(0, (rh - target_h) // 2)
    return resized[y0:y0 + target_h, x0:x0 + target_w]

def _add_sfx(clip_path, sfx_path, transition_frame, fps, volume, output_path):
    delay_ms = int((transition_frame / fps) * 1000)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(clip_path), "-i", str(sfx_path),
           "-filter_complex",
           f"[1:a]adelay={delay_ms}|{delay_ms},volume={volume}[sfx];[0:a][sfx]amix=inputs=2:duration=first[aout]",
           "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(output_path)]
    subprocess.run(cmd, check=True)

def _extract_background_frame(bg_video_path):
    import cv2
    cap = cv2.VideoCapture(str(bg_video_path))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"Impossible de lire le background: {bg_video_path}")
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

@app.function(image=image, gpu="T4", volumes={VIDEO_DIR: video_volume, MODEL_DIR: model_volume}, timeout=600)
def run_f00h_hook(input_uri, output_uri, campaign_id, preset="backrooms", hook_duration_seconds=2.0, canvas_width=None, canvas_height=None):
    import cv2, numpy as np, torch
    started = time.monotonic()
    config = _load_hook_config()
    backgrounds = config.get("backgrounds", {})
    sfx_map = config.get("sfx", {})
    default_sfx = config.get("default_sfx", "glitch")
    composition = config.get("composition", {})
    transition_frame = composition.get("transition_frame", 60)
    sfx_volume = composition.get("sfx_volume", 0.8)
    if preset not in backgrounds:
        raise ValueError(f"Preset inconnu: {preset}")
    bg_asset_rel = backgrounds[preset]["asset"]
    input_path = Path(VIDEO_DIR) / input_uri
    output_path = Path(VIDEO_DIR) / output_uri
    bg_video_path = Path(MODEL_DIR) / bg_asset_rel
    sfx_key = backgrounds[preset].get("sfx_key", default_sfx)
    sfx_rel = sfx_map.get(sfx_key, sfx_map.get(default_sfx, "sfx/glitch.mp3"))
    sfx_path = Path(MODEL_DIR) / sfx_rel
    output_path.parent.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(input_path))
    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
    src_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or OUTPUT_WIDTH)
    src_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or OUTPUT_HEIGHT)
    # Format de sortie : priorite a la config (canvas_width/height), sinon format source
    width = int(canvas_width) if canvas_width else src_width
    height = int(canvas_height) if canvas_height else src_height
    hook_frames = int(hook_duration_seconds * source_fps)
    predictor, device = _load_sam2_model()
    hook_bg_rgb = _extract_background_frame(bg_video_path)
    tmp_output = output_path.with_suffix(".hook.tmp.mp4")
    writer = cv2.VideoWriter(str(tmp_output), cv2.VideoWriter_fourcc(*"mp4v"), source_fps, (width, height))
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("VideoWriter indisponible")
    processed = 0
    with torch.inference_mode():
        while True:
            ok, frame_bgr = capture.read()
            if not ok:
                break
            if (width, height) != (src_width, src_height):
                frame_bgr = _cover_resize(frame_bgr, width, height)
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            if processed < hook_frames:
                mask = _segment_streamer(predictor, frame_rgb, device)
                composited = _composite_hook_frame(frame_rgb, hook_bg_rgb, mask, width, height)
                out_bgr = cv2.cvtColor(composited, cv2.COLOR_RGB2BGR)
            else:
                out_bgr = frame_bgr
            writer.write(out_bgr)
            processed += 1
    capture.release()
    writer.release()
    muxed = output_path.with_suffix(".mux.tmp.mp4")
    if sfx_path.is_file():
        _add_sfx(tmp_output, sfx_path, transition_frame, source_fps, sfx_volume, muxed)
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp_output), "-i", str(input_path),
                        "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "copy",
                        "-movflags", "+faststart", str(muxed)], check=True)
    muxed.replace(output_path)
    tmp_output.unlink(missing_ok=True)
    elapsed = time.monotonic() - started
    torch.cuda.empty_cache() if torch.cuda.is_available() else None
    return {"implementation": "f00h_sam2_hook", "stage": "F00H_HOOK", "preset": preset,
            "hook_duration_seconds": hook_duration_seconds, "hook_frames": hook_frames,
            "total_frames": processed, "resolution": [width, height], "source_resolution": [src_width, src_height], "fps": source_fps,
            "input": input_uri, "output": output_uri, "sfx_applied": sfx_path.is_file(),
            "inference_seconds": round(elapsed, 3)}
