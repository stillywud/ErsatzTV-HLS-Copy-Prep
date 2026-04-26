#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.wmv'}

# ── 编码模式 ──────────────────────────────────────────────────
# 'cpu'  = libx264 软编码（默认，兼容性最好，质量最高）
# 'amf'  = h264_amf AMD GPU 硬编码（RX580 等，速度极快）
# 'auto' = 自动检测：有 AMD AMF 则用 GPU，否则回退 CPU
ENCODER_MODE = 'auto'

# ── CPU 编码参数（libx264）──
FIXED_PRESET = 'medium'
FIXED_CRF = '23'

# ── AMF GPU 编码参数（h264_amf）──
AMF_USAGE = 'transcoding'          # transcoding / high_quality
AMF_QUALITY = 'speed'              # balanced / speed / quality - 使用speed减少体积
AMF_RATE_CONTROL = '2'             # 2 = vbr_peak (Variable Bitrate, Peak Constrained)
# 动态码率计算：根据源文件分辨率和码率自动计算
# 基础码率表（每像素比特率，经验值）
BITRATE_PER_PIXEL = {
    # 分辨率阈值 (宽度*高度): 目标码率 (bps)
    1280*720: 1500000,    # 720p: 1.5 Mbps
    1920*1080: 2500000,   # 1080p: 2.5 Mbps
    2560*1440: 4000000,   # 2K: 4 Mbps
    3840*2160: 8000000,   # 4K: 8 Mbps
}
# 最大码率倍数：输出不会超过源文件码率的这个倍数
MAX_BITRATE_RATIO = 1.2
# 最小码率保障（防止过低质量）
MIN_VIDEO_BITRATE = 800000  # 800 kbps
AMF_PREANALYSIS = False            # 禁用预分析
AMF_VBAQ = True                    # 启用 VBAQ
AMF_ENFORCE_HRD = False           # 不强制 HRD
AMF_ASYNC_DEPTH = 4               # 异步流水线深度
AMF_MAX_B_FRAMES = 0               # 禁用 B 帧
AMF_LEVEL = '4.0'                  # H.264 Level 4.0

# ── 通用参数 ──────────────────────────────────────────────────
FIXED_AUDIO_BITRATE = '160k'
FIXED_AUDIO_RATE = '48000'
FIXED_AUDIO_CHANNELS = '2'
FIXED_RESOLUTION_MODE = 'cap_1080p'
FIXED_PIXEL_FORMAT = 'yuv420p'
FIXED_PROFILE = 'high'
FIXED_LEVEL = 'auto'
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
KEYFRAME_SECONDS = 4  # GOP 与 ErsatzTV hls_time 对齐，减少文件大小
KEYFRAME_TOLERANCE = 0.5  # 4秒 GOP 下容差可以更小
FORCE_TRANSCODE = False   # 强制转码模式：跳过源文件合规检查和目标文件复用检查，所有文件一律重新转码
ETA_LOWER_MULTIPLIER = 0.8
ETA_UPPER_MULTIPLIER = 2.5
MOVE_RETRY_COUNT = 5
MOVE_RETRY_SECONDS = 2
TARGET_DURATION_TOLERANCE_SECONDS = 15
TARGET_DURATION_TOLERANCE_RATIO = 0.03


def timestamp() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def iso_now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def parse_rate(rate: Optional[str]) -> Optional[float]:
    if not rate or rate == '0/0':
        return None
    if '/' in rate:
        n, d = rate.split('/', 1)
        try:
            n = float(n)
            d = float(d)
            if d == 0:
                return None
            return n / d
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None


def parse_seconds(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_speed(text: str) -> Optional[float]:
    text = (text or '').strip().lower()
    if text.endswith('x'):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def format_bytes(num: int) -> str:
    value = float(num)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if value < 1024 or unit == 'TB':
            return f'{value:.2f} {unit}'
        value /= 1024
    return f'{num} B'


def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '未知'
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f'{h}小时{m}分{s}秒'
    if m > 0:
        return f'{m}分{s}秒'
    return f'{s}秒'


def safe_name(path: Path) -> str:
    return ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in str(path))


def resolve_root(value: Optional[str]) -> Path:
    if value:
        return Path(value).resolve()
    return Path(__file__).resolve().parent


def compute_target_resolution(src_width: int, src_height: int) -> tuple[int, int]:
    """Determine output resolution: cap at 1080p, keep original if below."""
    w, h = src_width, src_height
    if w > MAX_WIDTH or h > MAX_HEIGHT:
        scale = min(MAX_WIDTH / w, MAX_HEIGHT / h)
        w = int(w * scale)
        h = int(h * scale)
    w = w if w % 2 == 0 else w + 1
    h = h if h % 2 == 0 else h + 1
    return w, h


def build_vf(fps: float, src_width: int, src_height: int) -> str:
    fps_str = f'{fps:.6f}'.rstrip('0').rstrip('.')
    tw, th = compute_target_resolution(src_width, src_height)
    if tw != src_width or th != src_height:
        scale_part = f'scale={tw}:{th}'
    else:
        scale_part = f'scale=trunc(iw/2)*2:trunc(ih/2)*2'
    return f'{scale_part},setsar=1,fps={fps_str}'


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_videos(source_dir: Path, recursive: bool = True) -> list[Path]:
    iterator = source_dir.rglob('*') if recursive else source_dir.glob('*')
    return sorted(p for p in iterator if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def probe_keyframe_interval(ffprobe: Path, src: Path) -> Optional[float]:
    """Probe the maximum keyframe interval in seconds by sampling keyframe timestamps."""
    cmd = [
        str(ffprobe),
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'packet=pts_time,flags',
        '-of', 'csv',
        str(src),
    ]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
        if p.returncode != 0:
            return None
        last_kf = None
        max_gap = 0.0
        for line in p.stdout.strip().splitlines():
            parts = line.split(',')
            if len(parts) < 3:
                continue
            if 'K' in parts[2]:
                try:
                    pts = float(parts[1])
                except (ValueError, IndexError):
                    continue
                if last_kf is not None:
                    gap = pts - last_kf
                    if gap > max_gap:
                        max_gap = gap
                last_kf = pts
        return max_gap if max_gap > 0 else None
    except Exception:
        return None


def probe_media(ffprobe: Path, src: Path) -> dict:
    cmd = [
        str(ffprobe),
        '-v', 'error',
        '-show_entries', 'stream=index,codec_name,codec_type,avg_frame_rate,r_frame_rate,width,height,sample_aspect_ratio,channels,sample_rate,bit_rate,pix_fmt,profile:format=duration',
        '-of', 'json',
        str(src),
    ]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or 'ffprobe failed')
    info = json.loads(p.stdout)
    streams = info.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if not video:
        raise RuntimeError('未检测到视频流')
    avg_fps = parse_rate(video.get('avg_frame_rate'))
    real_fps = parse_rate(video.get('r_frame_rate'))
    fps = avg_fps or real_fps or 25.0
    duration = parse_seconds(info.get('format', {}).get('duration'))
    return {
        'video': video,
        'audio': audio,
        'fps': fps,
        'avg_fps': avg_fps,
        'real_fps': real_fps,
        'duration': duration,
    }


def analyze_meta(src: Path, meta: dict) -> dict:
    video = meta['video']
    audio = meta['audio']
    score = 0
    reasons = []

    video_codec = (video.get('codec_name') or '').lower()
    audio_codec = (audio.get('codec_name') or '').lower() if audio else None
    sar = video.get('sample_aspect_ratio')
    width = int(video.get('width') or 0)
    height = int(video.get('height') or 0)
    avg_fps = meta.get('avg_fps')
    real_fps = meta.get('real_fps')

    if video_codec != 'h264':
        score += 40
        reasons.append(f'视频编码为 {video_codec or "未知"}，不是 H.264')
    if audio is None:
        score += 5
        reasons.append('无音频流')
    elif audio_codec != 'aac':
        score += 20
        reasons.append(f'音频编码为 {audio_codec or "未知"}，不是 AAC')

    if src.suffix.lower() != '.mp4':
        score += 5
        reasons.append(f'封装格式为 {src.suffix.lower()}，不是 MP4')

    if width % 2 != 0 or height % 2 != 0:
        score += 10
        reasons.append('分辨率存在奇数边，需修正为偶数尺寸')

    if sar not in (None, '', '1:1', 'N/A', '0:1'):
        score += 10
        reasons.append(f'SAR 为 {sar}，不是 1:1')

    if avg_fps and real_fps and abs(avg_fps - real_fps) > 0.01:
        score += 15
        reasons.append('avg_frame_rate 与 r_frame_rate 不一致，疑似 VFR 或时序不规整')

    if (meta.get('fps') or 0) > 60:
        score += 10
        reasons.append(f'帧率较高：{meta.get("fps"):.3f}fps')
    elif (meta.get('fps') or 0) > 30:
        score += 5
        reasons.append(f'帧率偏高：{meta.get("fps"):.3f}fps')

    if width >= 3840 or height >= 2160:
        score += 10
        reasons.append('分辨率达到 4K 级别，离线处理耗时与体积可能更高')

    if score >= 50:
        level = '高'
    elif score >= 20:
        level = '中'
    else:
        level = '低'

    return {
        'risk_score': score,
        'risk_level': level,
        'reasons': reasons,
        'video_codec': video_codec or None,
        'audio_codec': audio_codec,
        'sar': sar,
    }


def print_banner() -> None:
    print('=' * 72)
    print('ErsatzTV HLS Copy 预处理工具（最终定版参数）')
    print('=' * 72)
    print('目标：离线一次性整理源文件，播放时继续使用 copy 模式')
    print('固定参数：H.264 / AAC / 1080p封顶 / 保持原fps但转CFR / 4秒GOP / 4秒强制关键帧 / 校验容差0.5秒')
    print('固定质量参数：preset=medium, crf=23, audio=160k, level=auto')
    print('')


def prompt_path(prompt: str, default: Path) -> Path:
    raw = input(f'{prompt} [{default}]: ').strip().strip('"')
    return Path(raw).resolve() if raw else default


def prompt_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = 'Y/n' if default else 'y/N'
    raw = input(f'{prompt} [{suffix}]: ').strip().lower()
    if not raw:
        return default
    return raw in {'y', 'yes', '1', 'true'}


def validate_tools(bin_dir: Path) -> tuple[Path, Path]:
    ffmpeg = bin_dir / 'ffmpeg.exe'
    ffprobe = bin_dir / 'ffprobe.exe'
    if not ffmpeg.exists():
        raise FileNotFoundError(f'缺少 ffmpeg.exe：{ffmpeg}')
    if not ffprobe.exists():
        raise FileNotFoundError(f'缺少 ffprobe.exe：{ffprobe}')
    return ffmpeg, ffprobe


def detect_encoder(ffmpeg: Path, mode: str = ENCODER_MODE) -> str:
    """Detect which video encoder to use based on mode setting.

    Returns 'h264_amf' if AMF GPU encoding is available and mode allows it,
    otherwise falls back to 'libx264'.
    """
    if mode == 'cpu':
        return 'libx264'

    # Try to check if h264_amf encoder is available
    try:
        result = subprocess.run(
            [str(ffmpeg), '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=15,
        )
        if 'h264_amf' in result.stdout:
            # Further verify: try a quick AMF init to confirm GPU is actually present
            # Use -f lavfi -i nullsrc to create a test frame and encode with h264_amf
            test_cmd = [
                str(ffmpeg), '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'nullsrc=s=256x256:d=0.1',
                '-c:v', 'h264_amf', '-f', 'null', '-',
            ]
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=30)
            if test_result.returncode == 0:
                if mode in ('amf', 'auto'):
                    return 'h264_amf'
    except Exception:
        pass

    if mode == 'amf':
        print(f'[警告] 指定了 AMF GPU 编码模式，但检测不到可用的 AMD GPU 硬件编码器。')
        print(f'       将回退到 CPU (libx264) 编码。')

    return 'libx264'


def get_encoder_display_name(encoder: str) -> str:
    if encoder == 'h264_amf':
        return 'AMD AMF GPU (h264_amf)'
    return 'CPU (libx264)'


def estimate_window(total_duration: float) -> tuple[float, float]:
    return total_duration * ETA_LOWER_MULTIPLIER, total_duration * ETA_UPPER_MULTIPLIER


def preflight(source_dir: Path, ffprobe: Path, recursive: bool = True) -> dict:
    files = list_videos(source_dir, recursive=recursive)
    if not files:
        return {
            'files': [],
            'count': 0,
            'total_size': 0,
            'total_duration': 0.0,
            'entries': [],
        }

    entries = []
    total_size = 0
    total_duration = 0.0
    for src in files:
        size = src.stat().st_size
        meta = probe_media(ffprobe, src)
        analysis = analyze_meta(src, meta)
        duration = meta['duration'] or 0.0
        fps = meta['fps']
        entry = {
            'path': src,
            'size': size,
            'duration': duration,
            'fps': fps,
            'width': meta['video'].get('width'),
            'height': meta['video'].get('height'),
            'avg_fps': meta.get('avg_fps'),
            'real_fps': meta.get('real_fps'),
            'has_audio': bool(meta['audio']),
            'video_codec': analysis['video_codec'],
            'audio_codec': analysis['audio_codec'],
            'sar': analysis['sar'],
            'risk_score': analysis['risk_score'],
            'risk_level': analysis['risk_level'],
            'reasons': analysis['reasons'],
        }
        entries.append(entry)
        total_size += size
        total_duration += duration

    return {
        'files': files,
        'count': len(files),
        'total_size': total_size,
        'total_duration': total_duration,
        'entries': entries,
    }


def progress_line(done: float, total: Optional[float], speed: Optional[float]) -> str:
    percent = '??%'
    eta = '未知'
    if total and total > 0:
        percent = f'{min(100.0, done / total * 100):5.1f}%'
        if speed and speed > 0:
            eta_seconds = max(0.0, (total - done) / speed)
            eta = format_seconds(eta_seconds)
    speed_text = f'{speed:.2f}x' if speed and speed > 0 else '未知'
    return f'    进度 {percent} | 已编码 {format_seconds(done)} / {format_seconds(total)} | 速度 {speed_text} | 预计剩余 {eta}'


def parse_out_time(line: str) -> Optional[float]:
    line = line.strip()
    if line.startswith('out_time_ms='):
        try:
            return int(line.split('=', 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith('out_time_us='):
        try:
            return int(line.split('=', 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith('out_time='):
        raw = line.split('=', 1)[1]
        parts = raw.split(':')
        if len(parts) == 3:
            try:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds
            except ValueError:
                return None
    return None


def is_duration_close(expected: Optional[float], actual: Optional[float]) -> bool:
    if expected is None or actual is None:
        return actual is not None and actual > 0
    tolerance = max(TARGET_DURATION_TOLERANCE_SECONDS, expected * TARGET_DURATION_TOLERANCE_RATIO)
    return abs(expected - actual) <= tolerance


def assess_source_compliance(meta: dict, max_keyframe_interval: Optional[float] = None) -> tuple[bool, list[str]]:
    """Check if the source file already meets all ErsatzTV copy-mode requirements.

    Returns (is_compliant, list_of_issues).
    If is_compliant=True, the file can be moved to done without transcoding.
    """
    issues = []
    video = meta.get('video') or {}
    audio = meta.get('audio')
    fps = meta.get('fps') or 0

    # ── Video codec must be h264 ──
    video_codec = (video.get('codec_name') or '').lower()
    if video_codec != 'h264':
        issues.append(f'视频编码为 {video_codec}，不是 h264')

    # ── Resolution must be even dimensions, within 1080p cap ──
    width = int(video.get('width') or 0)
    height = int(video.get('height') or 0)
    if width % 2 != 0 or height % 2 != 0:
        issues.append(f'分辨率 {width}x{height} 包含奇数边')
    expected_w, expected_h = compute_target_resolution(width, height)
    if width > expected_w or height > expected_h:
        issues.append(f'分辨率 {width}x{height} 超过 1080p 上限')

    # ── Pixel format must be yuv420p ──
    pix_fmt = video.get('pix_fmt') or ''
    if pix_fmt != FIXED_PIXEL_FORMAT:
        issues.append(f'像素格式为 {pix_fmt}，不是 {FIXED_PIXEL_FORMAT}')

    # ── SAR must be 1:1 (or empty/N/A) ──
    sar = video.get('sample_aspect_ratio') or ''
    if sar not in (None, '', '1:1', 'N/A', '0:1'):
        issues.append(f'SAR 为 {sar}，不是 1:1')

    # ── Keyframe interval must not exceed configured threshold with tolerance ──
    kf_threshold = KEYFRAME_SECONDS + KEYFRAME_TOLERANCE
    if max_keyframe_interval is not None and max_keyframe_interval > kf_threshold:
        issues.append(
            f'关键帧间隔为 {max_keyframe_interval:.1f}秒，超过校验阈值 {kf_threshold:.1f}秒（{KEYFRAME_SECONDS}秒+{KEYFRAME_TOLERANCE}秒容差）'
        )

    # ── Audio: if present, must be aac with correct params ──
    if audio is not None:
        audio_codec = (audio.get('codec_name') or '').lower()
        if audio_codec != 'aac':
            issues.append(f'音频编码为 {audio_codec}，不是 aac')

        audio_bitrate = parse_rate(audio.get('bit_rate') or '')
        expected_bitrate = parse_rate(FIXED_AUDIO_BITRATE)
        if audio_bitrate is not None and expected_bitrate is not None:
            if audio_bitrate > expected_bitrate:
                issues.append(
                    f'音频码率 {audio_bitrate:.0f} 高于目标上限 {expected_bitrate:.0f}'
                )

        audio_rate = int(audio.get('sample_rate') or 0)
        if audio_rate != int(FIXED_AUDIO_RATE):
            issues.append(f'音频采样率 {audio_rate}，不是 {FIXED_AUDIO_RATE}')

        audio_channels = int(audio.get('channels') or 0)
        if audio_channels != int(FIXED_AUDIO_CHANNELS):
            issues.append(f'音频声道 {audio_channels}，不是 {FIXED_AUDIO_CHANNELS}')

    return len(issues) == 0, issues


def validate_existing_target(
    ffprobe: Path,
    target: Path,
    src_meta: dict,
    encoder: str = 'libx264',
) -> tuple[bool, str, Optional[dict]]:
    if not target.exists() or target.stat().st_size <= 0:
        return False, '目标文件不存在或大小为 0', None
    try:
        meta = probe_media(ffprobe, target)
    except Exception as e:
        return False, f'无法探测目标文件：{e}', None
    if not meta.get('video'):
        return False, '目标文件缺少视频流', meta
    actual_duration = meta.get('duration')
    if not is_duration_close(src_meta.get('duration'), actual_duration):
        return False, (
            f'目标文件时长不匹配（源={format_seconds(src_meta.get("duration"))}，'
            f'目标={format_seconds(actual_duration)}）'
        ), meta
    params_match, mismatches = _compare_encoding_params(src_meta, meta, encoder)
    if not params_match:
        return False, '目标文件编码参数不匹配：' + '；'.join(mismatches), meta
    # ── Check keyframe interval (with tolerance) ──
    kf_threshold = KEYFRAME_SECONDS + KEYFRAME_TOLERANCE
    max_kf_interval = probe_keyframe_interval(ffprobe, target)
    if max_kf_interval is not None and max_kf_interval > kf_threshold:
        return False, (
            f'目标文件关键帧间隔为 {max_kf_interval:.1f}秒，超过校验阈值 {kf_threshold:.1f}秒（{KEYFRAME_SECONDS}秒+{KEYFRAME_TOLERANCE}秒容差）'
        ), meta
    return True, '目标文件校验通过（参数完全匹配，无需重新转码）', meta


def _compare_encoding_params(
    src_meta: dict,
    tgt_meta: dict,
    encoder: str = 'libx264',
) -> tuple[bool, list[str]]:
    mismatches = []
    tgt_video = tgt_meta.get('video') or {}
    src_video = src_meta.get('video') or {}

    tgt_video_codec = (tgt_video.get('codec_name') or '').lower()
    if tgt_video_codec != 'h264':
        mismatches.append(f'目标视频编码为 {tgt_video_codec}，不是 h264')

    src_width = int(src_video.get('width') or 0)
    src_height = int(src_video.get('height') or 0)
    tgt_width = int(tgt_video.get('width') or 0)
    tgt_height = int(tgt_video.get('height') or 0)
    expected_tw, expected_th = compute_target_resolution(src_width, src_height)
    if tgt_width != expected_tw or tgt_height != expected_th:
        mismatches.append(f'目标分辨率 {tgt_width}x{tgt_height}，期望 {expected_tw}x{expected_th}')

    src_fps = src_meta.get('fps') or 0
    tgt_fps = tgt_meta.get('fps') or 0
    if abs(tgt_fps - src_fps) > 0.01:
        mismatches.append(f'目标帧率 {tgt_fps:.3f}fps，源帧率 {src_fps:.3f}fps')

    tgt_audio = tgt_meta.get('audio')
    src_audio = src_meta.get('audio')
    has_audio = src_audio is not None

    if has_audio:
        tgt_audio_codec = (tgt_audio.get('codec_name') or '').lower() if tgt_audio else ''
        if tgt_audio_codec != 'aac':
            mismatches.append(f'目标音频编码为 {tgt_audio_codec}，不是 aac')
        tgt_bitrate_str = tgt_audio.get('bit_rate') or ''
        if tgt_bitrate_str:
            tgt_bitrate = parse_rate(tgt_bitrate_str)
            expected_bitrate = parse_rate(FIXED_AUDIO_BITRATE)
            if tgt_bitrate is not None and expected_bitrate is not None:
                if abs(tgt_bitrate - expected_bitrate) > 1000:
                    mismatches.append(f'目标音频码率 {tgt_bitrate:.0f}，期望 {expected_bitrate:.0f}')
        tgt_sample_rate = int(tgt_audio.get('sample_rate') or 0)
        expected_sample_rate = int(FIXED_AUDIO_RATE)
        if tgt_sample_rate != expected_sample_rate:
            mismatches.append(f'目标音频采样率 {tgt_sample_rate}，期望 {expected_sample_rate}')
        tgt_channels = int(tgt_audio.get('channels') or 0)
        expected_channels = int(FIXED_AUDIO_CHANNELS)
        if tgt_channels != expected_channels:
            mismatches.append(f'目标音频声道 {tgt_channels}，期望 {expected_channels}')
    elif tgt_audio is not None:
        mismatches.append('源无音频但目标有音频')

    tgt_pix_fmt = tgt_video.get('pix_fmt') or ''
    if tgt_pix_fmt != FIXED_PIXEL_FORMAT:
        mismatches.append(f'目标像素格式 {tgt_pix_fmt}，期望 {FIXED_PIXEL_FORMAT}')

    tgt_profile = (tgt_video.get('profile') or '').lower()
    expected_profile = FIXED_PROFILE.lower()
    if tgt_profile and tgt_profile != expected_profile:
        mismatches.append(f'目标 profile {tgt_profile}，期望 {expected_profile}')

    return len(mismatches) == 0, mismatches


def move_to_done(src: Path, done_root: Path, source_root: Path) -> Path:
    rel = src.relative_to(source_root)
    dst = done_root / rel
    if dst.exists():
        dst = dst.with_name(f'{dst.stem}-{timestamp()}{dst.suffix}')
    ensure_dir(dst.parent)

    last_error: Optional[Exception] = None
    for attempt in range(1, MOVE_RETRY_COUNT + 1):
        try:
            shutil.move(str(src), str(dst))
            return dst
        except Exception as e:
            last_error = e
            if attempt >= MOVE_RETRY_COUNT:
                break
            time.sleep(MOVE_RETRY_SECONDS)
    raise RuntimeError(f'原文件移动到 done 失败：{last_error}')


def compute_target_bitrate(src_width: int, src_height: int, src_bitrate: Optional[int]) -> tuple[str, str, str]:
    """Compute target bitrate based on source resolution and bitrate.
    
    Returns (target_bitrate, maxrate, bufsize) as strings with 'k' suffix.
    Ensures output does not exceed source bitrate * MAX_BITRATE_RATIO.
    """
    # 根据分辨率确定基础目标码率
    pixels = src_width * src_height
    base_bitrate = MIN_VIDEO_BITRATE
    
    # 找到适合的分辨率档位
    for threshold_pixels, threshold_bitrate in sorted(BITRATE_PER_PIXEL.items()):
        if pixels <= threshold_pixels:
            base_bitrate = threshold_bitrate
            break
    else:
        # 超过最大配置，使用4K码率
        base_bitrate = BITRATE_PER_PIXEL[3840*2160]
    
    # 如果源文件码率已知，限制不超过源码率的 MAX_BITRATE_RATIO 倍
    if src_bitrate and src_bitrate > 0:
        max_allowed = int(src_bitrate * MAX_BITRATE_RATIO)
        target_bitrate = min(base_bitrate, max_allowed)
    else:
        target_bitrate = base_bitrate
    
    # 确保不低于最小码率
    target_bitrate = max(target_bitrate, MIN_VIDEO_BITRATE)
    
    # 计算 maxrate (通常是目标码率的 1.6 倍) 和 bufsize
    maxrate = int(target_bitrate * 1.6)
    bufsize = int(target_bitrate * 2)
    
    # 转换为带 'k' 后缀的字符串 (kbps)
    def to_k(bps: int) -> str:
        return f'{bps // 1000}k'
    
    return to_k(target_bitrate), to_k(maxrate), to_k(bufsize)


def build_command(ffmpeg: Path, src: Path, dst: Path, fps: float, src_width: int, src_height: int, has_audio: bool, encoder: str = 'libx264', src_bitrate: Optional[int] = None) -> list[str]:
    gop = max(1, round(fps * KEYFRAME_SECONDS))
    tw, th = compute_target_resolution(src_width, src_height)

    common_prefix = [
        str(ffmpeg),
        '-y',
        '-hide_banner',
        '-loglevel', 'info',
        '-stats_period', '5',
        '-progress', 'pipe:1',
        '-i', str(src),
        '-map', '0:v:0',
        '-map', '0:a:0?',
        '-sn',
        '-dn',
        '-avoid_negative_ts', 'make_zero',
    ]

    if encoder == 'h264_amf':
        # ── AMD AMF GPU 编码路径 ──
        # AMF 输入需要 NV12，由 -vf 中的 format 转换；GPU 编码器自带 GOP 控制
        vf = build_vf(fps, src_width, src_height)
        # 动态计算码率
        target_bitrate, maxrate, bufsize = compute_target_bitrate(src_width, src_height, src_bitrate)
        cmd = common_prefix + [
            '-vf', f'{vf},format=nv12',
            '-c:v', 'h264_amf',
            '-usage', AMF_USAGE,
            '-quality', AMF_QUALITY,
            '-rc', AMF_RATE_CONTROL,
            '-b:v', target_bitrate,
            '-profile:v', 'high',
            '-level:v', AMF_LEVEL,
            '-maxrate', maxrate,
            '-bufsize', bufsize,
            '-g', str(gop),
            '-keyint_min', str(gop),
            '-sc_threshold', '0',
            '-bf', str(AMF_MAX_B_FRAMES),
            '-preanalysis', '1' if AMF_PREANALYSIS else '0',
            '-vbaq', '1' if AMF_VBAQ else '0',
            '-enforce_hrd', '1' if AMF_ENFORCE_HRD else '0',
            '-force_key_frames', f'expr:gte(t,n_forced*{KEYFRAME_SECONDS})',
            '-async_depth', str(AMF_ASYNC_DEPTH),
        ]
    else:
        # ── CPU libx264 编码路径（原逻辑） ──
        cmd = common_prefix + [
            '-vf', build_vf(fps, src_width, src_height),
            '-c:v', 'libx264',
            '-preset', FIXED_PRESET,
            '-crf', FIXED_CRF,
            '-pix_fmt', FIXED_PIXEL_FORMAT,
            '-profile:v', FIXED_PROFILE,
            '-g', str(gop),
            '-keyint_min', str(gop),
            '-sc_threshold', '0',
            '-bf', '0',
            '-force_key_frames', f'expr:gte(t,n_forced*{KEYFRAME_SECONDS})',
        ]

    if has_audio:
        cmd += [
            '-c:a', 'aac',
            '-b:a', FIXED_AUDIO_BITRATE,
            '-ar', FIXED_AUDIO_RATE,
            '-ac', FIXED_AUDIO_CHANNELS,
        ]
    else:
        cmd += ['-an']
    cmd += ['-movflags', '+faststart', str(dst)]
    return cmd


def run_one(ffmpeg: Path, src: Path, dst: Path, meta: dict, file_log: Path, encoder: str = 'libx264') -> tuple[int, Optional[Path], Optional[str]]:
    fps = meta['fps']
    duration = meta['duration']
    src_width = int(meta['video'].get('width') or 0)
    src_height = int(meta['video'].get('height') or 0)
    tw, th = compute_target_resolution(src_width, src_height)
    src_bitrate = int(meta['video'].get('bit_rate') or 0) if meta.get('video') else None
    cmd = build_command(ffmpeg, src, dst, fps, src_width, src_height, bool(meta['audio']), encoder=encoder, src_bitrate=src_bitrate)
    ensure_dir(dst.parent)
    ensure_dir(file_log.parent)

    with file_log.open('w', encoding='utf-8', newline='') as logf:
        logf.write(f'[{iso_now()}] SOURCE: {src}\n')
        logf.write(f'[{iso_now()}] TARGET: {dst}\n')
        logf.write(f'[{iso_now()}] ENCODER: {encoder}\n')
        logf.write(f'[{iso_now()}] FPS: {fps:.6f}\n')
        logf.write(f'[{iso_now()}] SOURCE_RESOLUTION: {src_width}x{src_height}\n')
        logf.write(f'[{iso_now()}] TARGET_RESOLUTION: {tw}x{th}\n')
        logf.write(f'[{iso_now()}] CMD: {json.dumps(cmd, ensure_ascii=False)}\n\n')
        logf.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )

        last_done = 0.0
        last_speed = None
        last_print = 0.0

        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip('\n')
            logf.write(raw_line)
            if line.startswith('out_time_') or line.startswith('out_time='):
                parsed = parse_out_time(line)
                if parsed is not None:
                    last_done = parsed
            elif line.startswith('speed='):
                last_speed = parse_speed(line.split('=', 1)[1])
            elif line.startswith('progress='):
                now = time.time()
                if now - last_print >= 1.5 or line.endswith('end'):
                    print(progress_line(last_done, duration, last_speed))
                    last_print = now
            logf.flush()

        proc.wait()
        if proc.returncode != 0:
            return proc.returncode, None, f'ffmpeg exited with code {proc.returncode}'
    return 0, dst, None


def default_paths(root: Path) -> dict:
    return {
        'bin': root / 'bin',
        'source': root / 'source',
        'target': root / 'target',
        'done': root / 'done',
        'logs': root / 'logs',
    }


def print_fixed_params(encoder: str = 'libx264') -> None:
    print('固定参数如下：')
    if encoder == 'h264_amf':
        print(f'  - 视频编码: h264_amf（AMD GPU 硬编码）')
        print(f'  - usage: {AMF_USAGE}')
        print(f'  - quality: {AMF_QUALITY}')
        print(f'  - 码率控制: {AMF_RATE_CONTROL}')
        print(f'  - 动态码率: 根据分辨率和源文件码率自动计算')
        print(f'  - preanalysis: {AMF_PREANALYSIS}')
        print(f'  - vbaq: {AMF_VBAQ}')
    else:
        print(f'  - 视频编码: libx264（CPU 软编码）')
        print(f'  - preset: {FIXED_PRESET}')
        print(f'  - crf: {FIXED_CRF}')
    print(f'  - 音频编码: aac')
    print(f'  - 音频码率: {FIXED_AUDIO_BITRATE}')
    print(f'  - 音频采样率: {FIXED_AUDIO_RATE}')
    print(f'  - 音频声道: {FIXED_AUDIO_CHANNELS}')
    print(f'  - 分辨率策略: 1080p封顶（超过则等比缩放，不超过则保持原分辨率，仅修正为偶数尺寸）')
    print(f'  - profile: {FIXED_PROFILE}')
    print(f'  - level: {FIXED_LEVEL}（自动选择）')
    print(f'  - 帧率策略: 保持原fps，输出为CFR')
    print(f'  - GOP: {KEYFRAME_SECONDS}秒')
    print(f'  - 强制关键帧: 每{KEYFRAME_SECONDS}秒')
    print(f'  - 关键帧校验容差: {KEYFRAME_TOLERANCE}秒（校验阈值: {KEYFRAME_SECONDS + KEYFRAME_TOLERANCE:.1f}秒）')
    print(f'  - 编码器模式: {ENCODER_MODE}（auto=自动检测GPU, cpu=强制CPU, amf=强制GPU）')
    print(f'  - 强制转码: {"开启（--force 或 FORCE_TRANSCODE=True）" if FORCE_TRANSCODE else "关闭"}')
    print(f'  - 其他: SAR=1:1, avoid_negative_ts=make_zero, movflags=+faststart')
    print('')


def prompt_common_paths(root: Path, include_target: bool = True) -> dict:
    paths = default_paths(root)
    for path in paths.values():
        ensure_dir(path)

    print('程序目录：')
    print(f'  {root}')
    print('')
    print('默认目录：')
    print(f'  source = {paths["source"]}')
    if include_target:
        print(f'  target = {paths["target"]}')
        print(f'  done   = {paths["done"]}')
    print(f'  logs   = {paths["logs"]}')
    print(f'  bin    = {paths["bin"]}')
    print('')

    selected = {
        'source': prompt_path('请输入源文件目录（直接回车使用默认 source）', paths['source']),
    }
    if include_target:
        selected['target'] = prompt_path('请输入输出目录（直接回车使用默认 target）', paths['target'])
        selected['done'] = prompt_path('请输入已处理原文件归档目录（直接回车使用默认 done）', paths['done'])
    selected['logs'] = prompt_path('请输入日志目录（直接回车使用默认 logs）', paths['logs'])
    selected['bin'] = prompt_path('请输入工具目录（直接回车使用默认 bin）', paths['bin'])

    if include_target:
        ensure_dir(selected['target'])
        ensure_dir(selected['done'])
    ensure_dir(selected['source'])
    ensure_dir(selected['logs'])
    return selected


def print_preflight_summary(pf: dict) -> None:
    lower_eta, upper_eta = estimate_window(pf['total_duration'])
    print(f'  文件数量：{pf["count"]}')
    print(f'  源文件总大小：{format_bytes(pf["total_size"])}')
    print(f'  视频总时长：{format_seconds(pf["total_duration"])}')
    print(f'  预估处理时间：{format_seconds(lower_eta)} ~ {format_seconds(upper_eta)}')
    print('  说明：这是基于总片长的经验估算，CPU/分辨率/磁盘速度会影响实际耗时。')
    print('')


def print_preflight_sample(pf: dict) -> None:
    sample = pf['entries'][:5]
    print('前几个待处理文件：')
    for item in sample:
        print(
            f'  - {item["path"].name} | '
            f'{item["width"]}x{item["height"]} | '
            f'{item["fps"]:.3f}fps | '
            f'{format_seconds(item["duration"])} | '
            f'{format_bytes(item["size"])} | '
            f'风险{item["risk_level"]}'
        )
    if pf['count'] > len(sample):
        print(f'  ... 还有 {pf["count"] - len(sample)} 个文件')
    print('')


def build_scan_report_data(source_dir: Path, logs_dir: Path, pf: dict) -> dict:
    sorted_entries = sorted(pf['entries'], key=lambda x: (-x['risk_score'], str(x['path']).lower()))
    high = sum(1 for x in pf['entries'] if x['risk_level'] == '高')
    medium = sum(1 for x in pf['entries'] if x['risk_level'] == '中')
    low = sum(1 for x in pf['entries'] if x['risk_level'] == '低')
    return {
        'generated_at': iso_now(),
        'source_dir': str(source_dir),
        'logs_dir': str(logs_dir),
        'fixed_params': {
            'preset': FIXED_PRESET,
            'crf': FIXED_CRF,
            'audio_bitrate': FIXED_AUDIO_BITRATE,
            'audio_rate': FIXED_AUDIO_RATE,
            'audio_channels': FIXED_AUDIO_CHANNELS,
            'resolution_mode': FIXED_RESOLUTION_MODE,
            'gop_seconds': KEYFRAME_SECONDS,
        },
        'summary': {
            'count': pf['count'],
            'total_size_bytes': pf['total_size'],
            'total_duration_seconds': pf['total_duration'],
            'risk_high': high,
            'risk_medium': medium,
            'risk_low': low,
        },
        'entries': [
            {
                'path': str(item['path']),
                'size_bytes': item['size'],
                'duration_seconds': item['duration'],
                'width': item['width'],
                'height': item['height'],
                'fps': item['fps'],
                'avg_fps': item['avg_fps'],
                'real_fps': item['real_fps'],
                'video_codec': item['video_codec'],
                'audio_codec': item['audio_codec'],
                'sar': item['sar'],
                'risk_score': item['risk_score'],
                'risk_level': item['risk_level'],
                'reasons': item['reasons'],
            }
            for item in sorted_entries
        ],
    }


def build_scan_report_text(report: dict) -> str:
    lines = []
    lines.append('ErsatzTV HLS Copy 预检查报告')
    lines.append('=' * 72)
    lines.append(f'生成时间：{report["generated_at"]}')
    lines.append(f'源目录：{report["source_dir"]}')
    lines.append('')
    lines.append('固定参数：')
    fp = report['fixed_params']
    lines.append(f'  preset={fp["preset"]}, crf={fp["crf"]}, audio={fp["audio_bitrate"]}, gop={fp["gop_seconds"]}秒')
    lines.append('')
    sm = report['summary']
    lines.append('统计：')
    lines.append(f'  文件数量：{sm["count"]}')
    lines.append(f'  总大小：{format_bytes(sm["total_size_bytes"])}')
    lines.append(f'  总时长：{format_seconds(sm["total_duration_seconds"])}')
    lines.append(f'  风险分布：高 {sm["risk_high"]} / 中 {sm["risk_medium"]} / 低 {sm["risk_low"]}')
    lines.append('')
    lines.append('重点关注文件：')
    for item in report['entries'][:15]:
        lines.append(
            f'- [{item["risk_level"]}|{item["risk_score"]:02d}] {Path(item["path"]).name} | '
            f'{item["width"]}x{item["height"]} | {item["fps"]:.3f}fps | '
            f'v={item["video_codec"] or "未知"} / a={item["audio_codec"] or "无"}'
        )
        if item['reasons']:
            for reason in item['reasons']:
                lines.append(f'    - {reason}')
        else:
            lines.append('    - 元数据看起来比较规整，但仍建议按固定参数统一预处理')
    lines.append('')
    lines.append('说明：')
    lines.append('  风险分数越高，只表示越值得优先检查或优先预处理；不是说低风险文件就一定可以跳过。')
    return '\n'.join(lines) + '\n'


def scan_only_mode(root: Path) -> int:
    print_banner()
    selected = prompt_common_paths(root, include_target=False)

    try:
        _, ffprobe = validate_tools(selected['bin'])
    except Exception as e:
        print(f'环境检查失败：{e}')
        return 2

    print('')
    print('[1/2] 环境检查通过，正在扫描源文件...')
    pf = preflight(selected['source'], ffprobe, recursive=True)
    if pf['count'] == 0:
        print('未在源目录中发现可处理的视频文件。')
        return 1

    print('[2/2] 扫描完成')
    print_preflight_summary(pf)
    print_preflight_sample(pf)

    sorted_entries = sorted(pf['entries'], key=lambda x: (-x['risk_score'], str(x['path']).lower()))
    print('最值得优先关注的文件：')
    for item in sorted_entries[:10]:
        reasons = '；'.join(item['reasons']) if item['reasons'] else '元数据相对规整'
        print(
            f'  - [{item["risk_level"]}|{item["risk_score"]:02d}] {item["path"].name} | '
            f'{item["width"]}x{item["height"]} | {item["fps"]:.3f}fps | '
            f'v={item["video_codec"] or "未知"} / a={item["audio_codec"] or "无"} | {reasons}'
        )
    print('')

    report = build_scan_report_data(selected['source'], selected['logs'], pf)
    report_id = timestamp()
    report_json = selected['logs'] / f'scan-{report_id}-report.json'
    report_txt = selected['logs'] / f'scan-{report_id}-report.txt'
    report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    report_txt.write_text(build_scan_report_text(report), encoding='utf-8')

    print('预检查报告已生成：')
    print(f'  - {report_txt}')
    print(f'  - {report_json}')
    print('')
    print('本模式不会执行转码，只做扫描、风险提示和耗时预估。')
    return 0


def guided_mode(root: Path) -> int:
    print_banner()
    selected = prompt_common_paths(root, include_target=True)

    try:
        ffmpeg, ffprobe = validate_tools(selected['bin'])
    except Exception as e:
        print(f'环境检查失败：{e}')
        return 2

    print('')
    print('[1/3] 环境检查通过')
    print(f'  ffmpeg  = {ffmpeg}')
    print(f'  ffprobe = {ffprobe}')

    encoder = detect_encoder(ffmpeg)
    print(f'  编码器  = {get_encoder_display_name(encoder)}')
    print_fixed_params(encoder)

    print('[2/3] 正在扫描源文件并预估处理时间，请稍候...')
    pf = preflight(selected['source'], ffprobe, recursive=True)
    if pf['count'] == 0:
        print('未在源目录中发现可处理的视频文件。')
        return 1

    print_preflight_summary(pf)
    print_preflight_sample(pf)

    sorted_entries = sorted(pf['entries'], key=lambda x: (-x['risk_score'], str(x['path']).lower()))
    print('优先关注的前几个文件：')
    for item in sorted_entries[:5]:
        reasons = '；'.join(item['reasons']) if item['reasons'] else '元数据相对规整'
        print(f'  - [{item["risk_level"]}|{item["risk_score"]:02d}] {item["path"].name} | {reasons}')
    print('')

    if not prompt_yes_no('[3/3] 是否开始处理', default=True):
        print('已取消。')
        return 0

    return execute_run(selected['source'], selected['target'], selected['done'], selected['logs'], ffmpeg, ffprobe, encoder, force=FORCE_TRANSCODE)


def execute_run(source_dir: Path, target_dir: Path, done_dir: Path, logs_dir: Path, ffmpeg: Path, ffprobe: Path, encoder: str = 'libx264', force: bool = False) -> int:
    files = list_videos(source_dir, recursive=True)
    if not files:
        print('源目录为空，没有可处理文件。')
        return 1

    run_id = timestamp()
    jsonl_log = logs_dir / f'run-{run_id}.jsonl'
    summary_json = logs_dir / f'run-{run_id}-summary.json'

    processed = 0
    failed = 0
    skipped = 0
    recovered = 0
    results = []

    print('')
    print('开始处理...')
    print(f'运行编号：{run_id}')
    print(f'汇总日志：{jsonl_log}')
    print(f'汇总报告：{summary_json}')
    print('')

    for idx, src in enumerate(files, start=1):
        rel = src.relative_to(source_dir)
        dst = (target_dir / rel).with_suffix('.mp4')
        file_log = logs_dir / f'{run_id}-{idx:04d}-{safe_name(rel)}.log'
        record = {
            'run_id': run_id,
            'index': idx,
            'source': str(src),
            'relative_source': str(rel),
            'target': str(dst),
            'log': str(file_log),
            'started_at': iso_now(),
            'status': 'unknown',
        }

        print(f'[{idx}/{len(files)}] {rel}')
        try:
            meta = probe_media(ffprobe, src)
            record.update({
                'source_width': meta['video'].get('width'),
                'source_height': meta['video'].get('height'),
                'source_fps': round(meta['fps'], 6),
                'duration_seconds': meta['duration'],
            })

            print(f'    输入: {meta["video"].get("width")}x{meta["video"].get("height")} | {meta["fps"]:.3f}fps | 时长 {format_seconds(meta["duration"])}')

            # ── Step 1: 判断 source 文件本身是否符合所有标准 ──
            if force:
                print('    [强制转码模式] 跳过合规检查，直接转码。')
            else:
                print('    正在探测关键帧间隔...')
                max_kf_interval = probe_keyframe_interval(ffprobe, src)
                if max_kf_interval is not None:
                    print(f'    关键帧间隔: {max_kf_interval:.1f}秒（配置要求: {KEYFRAME_SECONDS}秒）')
                src_compliant, src_issues = assess_source_compliance(meta, max_keyframe_interval=max_kf_interval)

                if src_compliant:
                    # 源文件已完全符合要求，直接移至 done，无需转码
                    print('    源文件已符合所有标准（无需转码），直接移至 done。')
                    done_path = move_to_done(src, done_dir, source_dir)
                    record['done_path'] = str(done_path)
                    record['status'] = 'ok_source_compliant'
                    record['note'] = '源文件已符合 HLS copy 模式全部要求，无需任何转码处理'
                    processed += 1
                    print(f'    结果: 符合标准，原文件已移动到 {done_path}')
                    continue

            if not force:
                # 源文件不符合标准，需要处理
                reasons_str = '；'.join(src_issues)
                print(f'    源文件不符合标准，原因：{reasons_str}')

                # ── Step 2: 检查 target 是否已存在且有效 ──
                existing_ok, existing_reason, existing_meta = validate_existing_target(ffprobe, dst, meta, encoder)
                if existing_ok:
                    print('    目标文件已存在且校验通过，本次不重复转码。')
                    done_path = move_to_done(src, done_dir, source_dir)
                    record['done_path'] = str(done_path)
                    record['status'] = 'recovered_existing_target'
                    record['note'] = existing_reason
                    if existing_meta:
                        record['target_duration_seconds'] = existing_meta.get('duration')
                    processed += 1
                    recovered += 1
                    print(f'    结果: 目标已存在无需转码，原文件已移动到 {done_path}')
                    continue

                if dst.exists():
                    print(f'    目标文件已存在但校验未通过：{existing_reason}')
                    print('    将重新转码并覆盖目标文件。')

            if dst.exists():
                print('    将重新转码并覆盖目标文件。')

            print(f'    输出: {dst}')
            print(f'    日志: {file_log}')
            rc, _, err = run_one(ffmpeg, src, dst, meta, file_log, encoder=encoder)
            if rc != 0:
                raise RuntimeError(err or f'ffmpeg exited with code {rc}')

            target_ok, target_reason, target_meta = validate_existing_target(ffprobe, dst, meta, encoder)
            if not target_ok:
                raise RuntimeError(f'转码完成后目标文件校验失败：{target_reason}')

            done_path = move_to_done(src, done_dir, source_dir)
            record['done_path'] = str(done_path)
            record['status'] = 'ok' if not force else 'ok_forced'
            record['target_duration_seconds'] = target_meta.get('duration') if target_meta else None
            processed += 1
            print(f'    结果: 成功，原文件已移动到 {done_path}')

        except Exception as e:
            failed += 1
            record['status'] = 'failed'
            record['error'] = str(e)
            print(f'    结果: 失败 -> {e}')

        record['finished_at'] = iso_now()
        with jsonl_log.open('a', encoding='utf-8', newline='') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        results.append(record)
        print('')

    summary = {
        'run_id': run_id,
        'source_dir': str(source_dir),
        'target_dir': str(target_dir),
        'done_dir': str(done_dir),
        'logs_dir': str(logs_dir),
        'encoder': encoder,
        'fixed_params': {
            'encoder': encoder,
            'preset': FIXED_PRESET,
            'crf': FIXED_CRF,
            'audio_bitrate': FIXED_AUDIO_BITRATE,
            'audio_rate': FIXED_AUDIO_RATE,
            'audio_channels': FIXED_AUDIO_CHANNELS,
            'resolution_mode': FIXED_RESOLUTION_MODE,
            'gop_seconds': KEYFRAME_SECONDS,
        },
        'processed': processed,
        'failed': failed,
        'skipped': skipped,
        'recovered_existing_target': recovered,
        'results': results,
    }
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')

    print('=' * 72)
    print('本次处理完成')
    print('=' * 72)
    print(f'成功：{processed}')
    print(f'其中免重复修复：{recovered}')
    print(f'失败：{failed}')
    print(f'跳过：{skipped}')
    print(f'汇总日志：{jsonl_log}')
    print(f'汇总报告：{summary_json}')
    print('')
    if failed:
        print('有失败文件，请优先查看对应 .log 文件。')
        return 2
    return 0


def check_only(root: Path) -> int:
    paths = default_paths(root)
    for path in paths.values():
        ensure_dir(path)
    try:
        ffmpeg, ffprobe = validate_tools(paths['bin'])
    except Exception as e:
        print(f'检查失败：{e}')
        return 2
    print_banner()
    print('环境检查通过。')
    print(f'ffmpeg:  {ffmpeg}')
    print(f'ffprobe: {ffprobe}')
    encoder = detect_encoder(ffmpeg)
    print(f'编码器:  {get_encoder_display_name(encoder)}')
    print_fixed_params(encoder)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Standalone preprocessing tool for ErsatzTV copy-mode HLS playback.')
    ap.add_argument('--root', default=None, help='Program root directory, default is script directory')
    ap.add_argument('--guided', action='store_true', help='Run interactive guided mode')
    ap.add_argument('--scan-only', action='store_true', help='Only scan files, estimate time, and generate risk report')
    ap.add_argument('--check-only', action='store_true', help='Only validate environment and print fixed parameters')
    ap.add_argument('--force', action='store_true', help='Force transcode all files, skip compliance check and existing target reuse')
    args = ap.parse_args()

    root = resolve_root(args.root)
    if args.check_only:
        return check_only(root)
    if args.scan_only:
        return scan_only_mode(root)
    # --force 命令行参数优先于 FORCE_TRANSCODE 常量
    if args.force:
        FORCE_TRANSCODE = True
    # 如果当前目录存在 .force 文件，也启用强制转码
    force_file = Path(__file__).parent / '.force'
    if force_file.exists():
        FORCE_TRANSCODE = True
    return guided_mode(root)


if __name__ == '__main__':
    sys.exit(main())
