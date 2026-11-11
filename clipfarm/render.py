"""Cut segments into vertical 9:16, join them, and burn in text + mix audio."""
from __future__ import annotations

from pathlib import Path

from . import ff

_amf_failed = False


def _vertical_filter(cfg: dict, src_w: int, src_h: int, punch: bool,
                     facecam: list[float] | None = None) -> str:
    v = cfg["video"]
    W, H, fps = v["width"], v["height"], v["fps"]
    if facecam:
        # streamer layout: face cam on top, gameplay below
        x, y, w, h = facecam
        top = int(H * float(v.get("facecam_height", 0.38))) // 2 * 2
        bot = H - top
        cam = (f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
               f"scale={W}:{top}:force_original_aspect_ratio=increase,crop={W}:{top}")
        game = f"scale=-2:{bot},crop={W}:{bot}:(iw-ow)*{float(v['crop_x'])}:0"
        chain = f"split[a][b];[a]{cam}[cam];[b]{game}[game];[cam][game]vstack"
    elif v["mode"] == "crop":
        x = f"(iw-ow)*{float(v['crop_x'])}"
        chain = f"scale=-2:{H},crop={W}:{H}:{x}:0"
    else:
        a = float(v["fg_aspect"])
        fg_w = min(src_w, int(src_h * a)) // 2 * 2
        fg = f"crop={fg_w}:ih,scale={W}:-2"
        bg = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,eq=brightness=-0.08"
        chain = f"split[a][b];[a]{bg}[bg];[b]{fg}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
    chain += f",fps={fps},setsar=1"
    if punch and v.get("punch_in", True):
        # zoom from 118% back to 100% over the first 0.5s
        chain += (f",zoompan=z='if(lt(it,0.5),1.18-0.36*it,1)'"
                  f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}")
    return chain + ",format=yuv420p"


def _video_codec(cfg: dict) -> list[str]:
    v = cfg["video"]
    if v.get("encoder") == "h264_amf" and not _amf_failed:
        # AMD GPU encoder (RX 400 series and newer): much faster than the CPU
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(v["crf"]), "-qp_p", str(v["crf"] + 2)]
    return ["-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"])]


def _encode(cfg: dict, before: list, after: list, cwd=None) -> None:
    """ffmpeg with the configured encoder; falls back to the CPU encoder if the GPU one fails."""
    global _amf_failed
    try:
        ff.run([*before, *_video_codec(cfg), *after], cwd=cwd)
    except RuntimeError:
        if cfg["video"].get("encoder") != "h264_amf" or _amf_failed:
            raise
        _amf_failed = True
        print("  AMD encoder unavailable - using the CPU encoder (slower)")
        ff.run([*before, *_video_codec(cfg), *after], cwd=cwd)


def segment(cfg: dict, src: str, start: float, dur: float, out: Path, punch: bool = False,
            facecam: list[float] | None = None) -> None:
    """Render src[start:start+dur] as a vertical clip with a uniform codec, so clips concat cleanly."""
    w, h = ff.video_size(src)
    vf = _vertical_filter(cfg, w, h, punch, facecam)
    args = ["-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", src]
    if ff.has_audio(src):
        amap = "0:a:0"
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a:0"
    args += ["-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", amap]
    _encode(cfg, args, ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                        "-shortest", str(out)])


def concat(parts: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out])
    lst.unlink()


def finish(cfg: dict, clip_dir: Path, ass_name: str, vo: Path | None,
           sfx: list[tuple[Path, float]]) -> Path:
    """rough.mp4 + overlay.ass (+ voiceover, + sound effects) -> final.mp4"""
    a = cfg["audio"]
    args = ["-i", "rough.mp4"]
    n = 1
    mix = []
    game_vol = a["game_volume_under_vo"] if vo else a["game_volume"]
    filters = [f"[0:v]ass={ass_name}[v]", f"[0:a]volume={game_vol}[g]"]
    mix.append("[g]")
    if vo:
        args += ["-i", str(Path(vo).resolve())]
        filters.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.0[vo]")
        mix.append("[vo]")
        n += 1
    for i, (path, at) in enumerate(sfx):
        args += ["-i", str(path.resolve())]
        ms = int(at * 1000)
        filters.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,"
                       f"adelay={ms}|{ms},volume=0.8[s{i}]")
        mix.append(f"[s{i}]")
        n += 1
    if len(mix) > 1:
        filters.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        filters.append("[g]alimiter=limit=0.95[a]")
    args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]"]
    _encode(cfg, args, ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "final.mp4"],
            cwd=clip_dir)
    return clip_dir / "final.mp4"
