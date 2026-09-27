"""GIF / 动图处理模块。

这里的函数只做纯计算，不接触 AstrBot 事件对象，因此可以整体丢进线程池执行。
处理失败统一抛出 GifToolboxError，其异常文本可直接展示给用户。
"""

from __future__ import annotations

import io
import random
import re
from dataclasses import dataclass, replace

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageSequence
from astrbot.api import logger

try:
    import imageio
except ImportError:  # pragma: no cover - 缺失时仅「视频转gif」不可用
    imageio = None


# GIF 的最小帧间隔是 20ms，等效上限 50fps；想要更高只能靠抽帧模拟。
MIN_FRAME_MS = 20
MAX_FPS = 50
MAX_FRAME_MS = 10000
MAX_GRID = 20
MAX_DECOMPOSE_FRAMES = 100
MAX_CONCAT_FRAMES = 500
MAX_VIDEO_FRAMES = 400
# 视频转动图的输出超过这个体积就降配重试一次，避免平台把动图转成静态图。
VIDEO_COMPRESS_THRESHOLD_MB = 10.0
MAX_AGE_TIMES = 50

# Pillow 的 Image.ADAPTIVE 在部分版本被收进 Image.Palette，取值都是 1。
_ADAPTIVE_PALETTE = getattr(getattr(Image, "Palette", None), "ADAPTIVE", 1)

SUPPORTED_FORMATS = ("GIF", "APNG", "WEBP")


class GifToolboxError(Exception):
    """处理失败，异常文本为可直接展示给用户的提示。"""


@dataclass(frozen=True)
class VideoOptions:
    """视频转动图的可调参数。"""

    output_format: str = "GIF"
    default_scale: float = 0.3
    default_fps: int = 10
    max_duration: float = 10.0
    max_colors: int = 256


def is_video_supported() -> bool:
    return imageio is not None


def normalize_format(output_format: str) -> str:
    fmt = (output_format or "GIF").upper()
    return fmt if fmt in SUPPORTED_FORMATS else "GIF"


# --- 帧读写基础 ---


def _frame_duration(frame: Image.Image, default_ms: int = 100) -> int:
    """读取单帧时长，缺失或非法时回落到 default_ms。"""
    try:
        duration = int(frame.info.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        duration = default_ms
    return max(MIN_FRAME_MS, min(duration, MAX_FRAME_MS))


def _read_frames(img: Image.Image) -> tuple[list[Image.Image], list[int]]:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        durations.append(_frame_duration(frame))
        frames.append(frame.copy())
    return frames, durations


def _open_animation(img_data: bytes, action: str) -> Image.Image:
    img = Image.open(io.BytesIO(img_data))
    if not getattr(img, "is_animated", False):
        raise GifToolboxError(f"这不是动图，无法{action}")
    return img


def _resample(frames: list[Image.Image], keep: int) -> list[Image.Image]:
    """均匀抽帧到 keep 帧。"""
    total = len(frames)
    keep = max(2, min(keep, total))
    return [frames[int(index * total / keep)] for index in range(keep)]


def save_animation(
    frames: list[Image.Image],
    durations: list[int],
    output_format: str = "GIF",
    loop: int = 0,
) -> bytes:
    """保存动图。只有 GIF 支持逐帧时长，APNG/WebP 退化为平均时长。"""
    if not frames:
        raise GifToolboxError("没有可保存的帧")
    fmt = normalize_format(output_format)
    if fmt != "GIF":
        frames = [f.convert("RGBA") if f.mode in ("P", "PA") else f for f in frames]

    average = int(round(sum(durations) / len(durations)))
    per_frame = durations if (fmt == "GIF" and len(durations) == len(frames)) else average
    kwargs = {
        "save_all": True,
        "append_images": frames[1:],
        "duration": per_frame,
        "loop": loop,
    }

    output = io.BytesIO()
    if fmt == "APNG":
        frames[0].save(output, format="PNG", optimize=True, default_image=True, **kwargs)
    elif fmt == "WEBP":
        frames[0].save(output, format="WEBP", method=3, quality=80, **kwargs)
    else:
        frames[0].save(output, format="GIF", optimize=True, disposal=2, **kwargs)
    return output.getvalue()


def _save_gif_shared_palette(
    frames: list[Image.Image], durations: list[int], loop: int = 0
) -> bytes:
    """用共享调色板保存 GIF，保留透明区域。

    逐帧独立量化会让各帧颜色漂移，透明像素还可能被填成绿色；这里统一取前
    16 帧的颜色建一张调色板，并把索引 255 固定留作透明色。
    """
    frames = [frame.convert("RGBA") for frame in frames]
    has_alpha = any(frame.getchannel("A").getextrema()[0] < 255 for frame in frames)
    width, height = frames[0].size

    sample_width = width * min(len(frames), 16)
    sample = Image.new("RGB", (sample_width, height), (255, 255, 255))
    for index, frame in enumerate(frames[:16]):
        sample.paste(frame.convert("RGB"), (index * width, 0))
    palette = sample.quantize(colors=255 if has_alpha else 256, method=1)

    quantized = []
    for frame in frames:
        candidate = frame.convert("RGB").quantize(palette=palette)
        if has_alpha:
            mask = frame.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0)
            candidate.paste(255, mask=mask)
        quantized.append(candidate)

    kwargs = {
        "format": "GIF",
        "save_all": True,
        "append_images": quantized[1:],
        "duration": durations,
        "loop": loop,
        "disposal": 2,
        "optimize": not has_alpha,
    }
    if has_alpha:
        kwargs["transparency"] = 255
        kwargs["background"] = 255
    output = io.BytesIO()
    quantized[0].save(output, **kwargs)
    return output.getvalue()


# --- 变速 ---


def change_speed(
    img_data: bytes, value: float, is_fps_mode: bool, output_format: str = "GIF"
) -> bytes:
    """动图变速。

    value 在倍速模式下是速度倍数，在帧率模式下是目标 fps。目标帧率超过
    GIF 的 50fps 上限时改为抽帧，用 20ms 帧间隔模拟更快的视觉节奏。
    """
    if value <= 0:
        raise GifToolboxError("数值必须大于 0")

    frames, durations = _read_frames(_open_animation(img_data, "变速"))
    if not frames:
        raise GifToolboxError("没有读取到有效帧")

    if is_fps_mode:
        if value > MAX_FPS:
            frames = _resample(frames, int(len(frames) * MAX_FPS / value))
        durations = [max(MIN_FRAME_MS, int(1000.0 / value))] * len(frames)
    else:
        scaled = [min(MAX_FRAME_MS, max(1, int(d / value))) for d in durations]
        average = sum(scaled) / len(scaled)
        if average < MIN_FRAME_MS:
            frames = _resample(frames, int(len(frames) * average / MIN_FRAME_MS))
            durations = [MIN_FRAME_MS] * len(frames)
        else:
            durations = scaled

    return save_animation(frames, durations, output_format)


# --- 倒放 ---


def reverse_gif(img_data: bytes) -> bytes:
    img = _open_animation(img_data, "倒放")
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        durations.append(_frame_duration(frame))
        frames.append(frame.copy().convert("RGBA"))
    if len(frames) < 2:
        raise GifToolboxError("只有一帧，不需要倒放")

    frames.reverse()
    durations.reverse()
    return _save_gif_shared_palette(frames, durations)


# --- 分解 ---


def decompose_gif(img_data: bytes) -> list[bytes]:
    """把动图拆成单帧 PNG。"""
    img = _open_animation(img_data, "分解")
    frames = []
    for index, frame in enumerate(ImageSequence.Iterator(img)):
        if index >= MAX_DECOMPOSE_FRAMES:
            break
        output = io.BytesIO()
        frame.copy().convert("RGBA").save(output, format="PNG")
        frames.append(output.getvalue())
    if not frames:
        raise GifToolboxError("没有读取到有效帧")
    return frames


# --- 网格裁剪 ---

_MARGIN_PATTERN = r"边距\s*([上下左右])?边?\s*(\d+)"
_MARGIN_KEYS = {"上": "top", "下": "bottom", "左": "left", "右": "right"}
_GRID_PATTERN = r"(\d+)\s*[*x×]\s*(\d+)"


def parse_margins(text: str) -> tuple[str, dict[str, int]]:
    """解析 `边距上20` 一类参数，返回剔除这些片段后的文本与四边像素值。"""
    margins = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    for direction, amount in re.findall(_MARGIN_PATTERN, text):
        value = int(amount)
        if direction:
            margins[_MARGIN_KEYS[direction]] += value
        else:
            for key in margins:
                margins[key] += value
    return re.sub(_MARGIN_PATTERN, " ", text), margins


def parse_grid(text: str) -> tuple[int, int]:
    """解析 `3x4` 一类的网格参数，返回 (行数, 列数)。"""
    match = re.search(_GRID_PATTERN, text)
    if not match:
        return 1, 1
    return int(match.group(1)), int(match.group(2))


def _apply_margins(img_data: bytes, margins: dict[str, int]) -> bytes:
    img = Image.open(io.BytesIO(img_data)).convert("RGBA")
    width, height = img.size
    box = (
        margins["left"],
        margins["top"],
        width - margins["right"],
        height - margins["bottom"],
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise GifToolboxError(f"边距超出图片尺寸 {width}x{height}，裁不出有效区域")

    output = io.BytesIO()
    img.crop(box).save(output, format="PNG")
    return output.getvalue()


def crop_grid(
    img_data: bytes, rows: int, cols: int, margins: dict[str, int] | None = None
) -> list[bytes]:
    """按 rows x cols 网格切图，可先裁掉四周边距。"""
    if rows < 1 or cols < 1:
        raise GifToolboxError("行列数必须大于 0")
    if rows > MAX_GRID or cols > MAX_GRID:
        raise GifToolboxError(f"最多支持 {MAX_GRID}x{MAX_GRID} 的网格")

    if margins and any(margins.values()):
        img_data = _apply_margins(img_data, margins)

    img = Image.open(io.BytesIO(img_data)).convert("RGBA")
    width, height = img.size
    cell_width, cell_height = width // cols, height // rows
    if cell_width < 1 or cell_height < 1:
        raise GifToolboxError(f"图片只有 {width}x{height}，切不出 {rows}x{cols} 格")

    pieces = []
    for row in range(rows):
        for col in range(cols):
            output = io.BytesIO()
            img.crop(
                (
                    col * cell_width,
                    row * cell_height,
                    (col + 1) * cell_width,
                    (row + 1) * cell_height,
                )
            ).save(output, format="PNG")
            pieces.append(output.getvalue())
    return pieces


# --- 图片转线稿 ---


def to_line_art(img_data: bytes) -> bytes:
    """本地线稿算法：灰度 → 边缘检测 → 反相 → 提高对比度。"""
    try:
        gray = Image.open(io.BytesIO(img_data)).convert("L")
        edges = ImageOps.invert(gray.filter(ImageFilter.FIND_EDGES))
        result = ImageEnhance.Contrast(edges).enhance(3.0)
    except Exception as exc:
        raise GifToolboxError(f"线稿转换失败：{exc}") from exc

    output = io.BytesIO()
    result.save(output, format="JPEG", quality=90)
    return output.getvalue()


# --- 表情包做旧 ---

_AGE_GREEN_INTERVAL = 3
_AGE_BLUR_INTERVAL = 3
_AGE_SHARPEN_INTERVAL = 5
_AGE_PIXELATE_THRESHOLD = 15


def _shift_to_green(img: Image.Image) -> Image.Image:
    red, green, blue = img.split()
    boost = random.randint(1, 2)
    green = green.point(lambda value: min(255, value + boost))
    red_drop = random.randint(0, 1)
    blue_drop = random.randint(0, 1)
    if red_drop:
        red = red.point(lambda value: max(0, value - red_drop))
    if blue_drop:
        blue = blue.point(lambda value: max(0, value - blue_drop))
    return Image.merge("RGB", (red, green, blue))


def _pixelate(img: Image.Image, ratio: float) -> Image.Image:
    width, height = img.size
    if width <= 50 or height <= 50:
        return img
    small = img.resize((int(width * ratio), int(height * ratio)), Image.Resampling.BILINEAR)
    return small.resize((width, height), Image.Resampling.BILINEAR)


def _age_single_frame(img: Image.Image, times: int) -> Image.Image:
    """渐进式做旧：绿通道偏移 + JPEG 反复压缩 + 模糊/锐化 + 降饱和降对比。"""
    img = img.convert("RGB")
    for round_index in range(times):
        if round_index % _AGE_GREEN_INTERVAL == 0:
            img = _shift_to_green(img)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=max(25, 70 - round_index * 3))
        img = Image.open(buffer).convert("RGB")

        if round_index % _AGE_BLUR_INTERVAL == 0:
            radius = 0.2 + (round_index // _AGE_BLUR_INTERVAL) * 0.1
            img = img.filter(ImageFilter.GaussianBlur(radius=radius))
        if round_index % _AGE_SHARPEN_INTERVAL == 2:
            img = img.filter(ImageFilter.SHARPEN)
        if round_index % 2 == 0:
            img = ImageEnhance.Color(img).enhance(0.985)
        else:
            img = ImageEnhance.Contrast(img).enhance(0.99)
        if times >= _AGE_PIXELATE_THRESHOLD and round_index == times // 2:
            img = _pixelate(img, 0.8)
    return img


def age_meme(img_data: bytes, times: int) -> bytes:
    """表情包做旧，模拟早期互联网反复转发的失真效果；动图逐帧处理。"""
    times = max(1, min(times, MAX_AGE_TIMES))
    img = Image.open(io.BytesIO(img_data))

    if getattr(img, "is_animated", False):
        frames, durations = [], []
        for frame in ImageSequence.Iterator(img):
            durations.append(_frame_duration(frame))
            aged = _age_single_frame(frame.copy(), times)
            frames.append(aged.convert("P", palette=_ADAPTIVE_PALETTE, colors=256))
        if not frames:
            raise GifToolboxError("动图帧读取失败")
        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        return output.getvalue()

    quality = max(30, 70 - times * 3)
    output = io.BytesIO()
    _age_single_frame(img, times).save(output, format="JPEG", quality=quality)
    return output.getvalue()


# --- 表情合成 ---


def _fit_to_canvas(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """等比缩放后居中贴到统一画布上，空白处保持透明。"""
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 0))
    ratio = img.width / img.height
    if ratio > canvas_w / canvas_h:
        new_w, new_h = canvas_w, max(1, int(canvas_w / ratio))
    else:
        new_h, new_w = canvas_h, max(1, int(canvas_h * ratio))
    resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas.paste(resized, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2), mask=resized)
    return canvas


def _decimate_durations(durations: list[int], max_frames: int) -> tuple[list[int], list[int]]:
    """帧数超限时等比抽帧，被丢帧的时长并入保留帧，保证总时长不变。"""
    total = len(durations)
    if total <= max_frames:
        return list(range(total)), list(durations)

    step = total / max_frames
    indices: list[int] = []
    kept: list[int] = []
    position = 0.0
    while int(position) < total:
        start = int(position)
        end = min(total, max(int(position + step), start + 1))
        if len(indices) >= max_frames:
            kept[-1] += sum(durations[start:total])
            break
        indices.append(start)
        kept.append(sum(durations[start:end]))
        position += step
    return indices, kept


def concat_images(
    images: list[bytes], duration_sec: float, output_format: str = "GIF"
) -> bytes:
    """把多张表情按输入顺序接成一条动图。

    动图逐帧展开并沿用各帧原本的时长，静态图占一帧、时长取 duration_sec。
    所有帧统一到最大画布，等比缩放后居中填充。
    """
    default_ms = max(MIN_FRAME_MS, int(duration_sec * 1000))

    plans: list[tuple[bytes, list[int]]] = []
    canvas_w = canvas_h = 0
    for data in images:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                canvas_w = max(canvas_w, probe.width)
                canvas_h = max(canvas_h, probe.height)
                if getattr(probe, "is_animated", False):
                    durations = []
                    for index in range(getattr(probe, "n_frames", 1)):
                        probe.seek(index)
                        durations.append(_frame_duration(probe, default_ms))
                else:
                    durations = [default_ms]
                plans.append((data, durations))
        except Exception as exc:
            logger.warning("表情合成：跳过无法解析的图片: %s", exc)

    if not plans:
        raise GifToolboxError("没有可用的图片")

    total_frames = sum(len(durations) for _, durations in plans)
    trimmed = total_frames > MAX_CONCAT_FRAMES

    frames: list[Image.Image] = []
    durations: list[int] = []
    for data, frame_durations in plans:
        if trimmed:
            allow = max(1, int(len(frame_durations) * MAX_CONCAT_FRAMES / total_frames))
            indices, frame_durations = _decimate_durations(frame_durations, allow)
        else:
            indices = list(range(len(frame_durations)))
        try:
            with Image.open(io.BytesIO(data)) as source:
                for index, ms in zip(indices, frame_durations):
                    source.seek(index)
                    frames.append(_fit_to_canvas(source.convert("RGBA"), canvas_w, canvas_h))
                    durations.append(ms)
        except Exception as exc:
            logger.warning("表情合成：展开图片帧失败: %s", exc)

    if not frames:
        raise GifToolboxError("没有可合成的帧")

    return save_animation(frames, durations, output_format)


# --- 视频转动图 ---


def parse_video_args(text: str, options: VideoOptions) -> dict:
    """解析视频转动图参数：时间区间、抽帧步长、帧率、缩放。"""
    params = {
        "start": 0.0,
        "end": None,
        "fps": options.default_fps,
        "step": 1,
        "scale": options.default_scale,
        "force_step": False,
    }

    time_range = re.search(r"(\d+(?:\.\d+)?)[sS]?\s*[-~]\s*(\d+(?:\.\d+)?)[sS]?", text)
    if time_range:
        params["start"] = float(time_range.group(1))
        params["end"] = float(time_range.group(2))
        text = text.replace(time_range.group(0), " ")
    else:
        start = re.search(r"(?:开始|start)\s*(\d+(?:\.\d+)?)", text)
        length = re.search(r"(?:时长|len|time)\s*(\d+(?:\.\d+)?)", text)
        if start:
            params["start"] = float(start.group(1))
        if length:
            params["end"] = params["start"] + float(length.group(1))

    # `1/3` 表示每 3 帧取 1 帧，优先于 fps。
    step = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if step:
        params["step"] = max(1, int(step.group(1)), int(step.group(2)))
        params["fps"] = None
        params["force_step"] = True
        text = text.replace(step.group(0), " ")
    else:
        fps = re.search(r"(?:fps|帧率)\s*(\d+)", text)
        if fps:
            params["fps"] = int(fps.group(1))

    scale = re.search(r"\b(0\.\d+|1\.0)\b", text)
    if scale:
        params["scale"] = float(scale.group(1))
    params["scale"] = min(1.0, max(0.1, params["scale"]))
    return params


def _extract_video_frames(
    video_path: str, params: dict, options: VideoOptions
) -> tuple[list[Image.Image], float]:
    """按参数抽帧，返回 (帧列表, 目标帧率)。"""
    reader = imageio.get_reader(video_path, format="FFMPEG")
    try:
        meta = reader.get_meta_data()
        video_duration = float(meta.get("duration") or 0)
        source_fps = float(meta.get("fps") or 30) or 30.0

        start = float(params["start"])
        if start >= video_duration:
            raise GifToolboxError(f"开始时间 {start:g}s 超出视频长度 {video_duration:.1f}s")
        end = video_duration if params["end"] is None else float(params["end"])
        end = min(end, start + options.max_duration, video_duration)

        if params.get("force_step"):
            step = int(params["step"])
            target_fps = source_fps / step
        elif params.get("fps"):
            target_fps = min(float(params["fps"]), source_fps)
            step = max(1, int(source_fps / target_fps))
        else:
            step = 3
            target_fps = source_fps / step

        fmt = normalize_format(options.output_format)
        scale = float(params["scale"])
        frames: list[Image.Image] = []
        for index, raw in enumerate(reader):
            moment = index / source_fps
            if moment < start:
                continue
            if moment > end:
                break
            if index % step:
                continue
            frame = Image.fromarray(raw)
            frame = frame.resize(
                (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                Image.Resampling.BILINEAR,
            )
            if fmt == "GIF" and options.max_colors < 256:
                frame = frame.quantize(
                    colors=options.max_colors, method=1, dither=Image.Dither.FLOYDSTEINBERG
                )
            frames.append(frame)
            if len(frames) >= MAX_VIDEO_FRAMES:
                break
    finally:
        reader.close()

    if not frames:
        raise GifToolboxError("没有抽取到任何帧，换个时间区间试试")
    return frames, target_fps


def _render_video(video_path: str, params: dict, options: VideoOptions) -> bytes:
    frames, target_fps = _extract_video_frames(video_path, params, options)
    duration = max(1, int(1000 / target_fps)) if target_fps > 0 else 100
    return save_animation(frames, [duration] * len(frames), options.output_format)


def video_to_gif(video_path: str, params: dict, options: VideoOptions) -> bytes:
    if imageio is None:
        raise GifToolboxError(
            "缺少 imageio 依赖，无法转换视频。请执行：pip install imageio imageio-ffmpeg"
        )

    data = _render_video(video_path, params, options)

    # 输出过大时降低缩放与色数重试一次，避免平台把动图转成静态图。
    size_mb = len(data) / 1024 / 1024
    if size_mb > VIDEO_COMPRESS_THRESHOLD_MB and normalize_format(options.output_format) == "GIF":
        retry_params = {**params, "scale": max(0.1, round(params["scale"] * 0.8, 2))}
        retry_options = replace(options, max_colors=128 if options.max_colors > 128 else 64)
        try:
            retry_data = _render_video(video_path, retry_params, retry_options)
        except GifToolboxError as exc:
            logger.warning("视频压缩重试失败: %s", exc)
            return data
        if len(retry_data) < len(data):
            return retry_data

    return data
