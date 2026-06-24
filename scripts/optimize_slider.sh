#!/usr/bin/env bash
# =============================================================================
# optimize_slider.sh — 压缩首页 carousel 图片（保留原图 + 生成 thumb）
#
# 用法:
#   ./scripts/optimize_slider.sh images/slider/<某张图.jpg>
#   ./scripts/optimize_slider.sh images/slider/*.jpg   # 批量
#
# 干的事:
#   1. 原图保持不动（保留所有像素，click-to-zoom 用）
#   2. 生成轻压版到 images/slider/thumbs/<name>.jpg（保留原图分辨率，JPEG 92%）
#      - 不再限制长边：原图所有像素都保留，最大支持 4K/5K 屏
#      - 92% JPEG 视觉上几乎无损，文件大小只有原图 50-70%
#      - click-to-zoom 仍指向原图（更清晰）
#
# 文件结构（前后对比）:
#   之前: images/slider/event13.JPG （被覆盖压缩）
#   之后:
#     images/slider/event13.JPG          ← 原图（保留）
#     images/slider/thumbs/event13.jpg  ← 首页用的压缩版
#
# 依赖: python3 + Pillow（pip install Pillow）
# =============================================================================
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "用法: $0 <image1> [image2 ...]"
  echo "示例: $0 images/slider/new_party_photo.jpg"
  exit 1
fi

# 校验 Pillow
if ! python3 -c "import PIL" 2>/dev/null; then
  echo "❌ 需要 Pillow。装一下: pip install Pillow"
  exit 1
fi

python3 - "$@" <<'PYEOF'
import sys
from pathlib import Path
from PIL import Image

# MAX_LONG_EDGE: 不再限制长边 — 保留原图所有像素以保证最高画质
#                如果将来需要限制，把下面这行取消注释并改数字
# MAX_LONG_EDGE = 2400
JPEG_QUALITY = 92

def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

for arg in sys.argv[1:]:
    src = Path(arg)
    if not src.exists():
        print(f"  ⚠️  跳过（不存在）: {src}")
        continue

    # thumb 输出路径: images/slider/thumbs/<basename>.jpg
    thumb_dir = src.parent / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb = thumb_dir / (src.stem + ".jpg")

    # 若 thumb 比原图新，跳过（除非原图比 thumb 新 → 强制重压）
    if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
        print(f"  ⏭️  跳过（thumb 已存在且更新）: {src.name} → {thumb.name}")
        continue

    orig_size = src.stat().st_size
    try:
        img = Image.open(src)
    except Exception as e:
        print(f"  ⚠️  跳过（不是图片）: {src}  ({e})")
        continue

    # 缩放（保持宽高比）— 默认不缩放（保留原图所有像素）
    w, h = img.size
    long_edge = max(w, h)
    if "MAX_LONG_EDGE" in dir() and MAX_LONG_EDGE and long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)
        resized = True
    else:
        resized = False

    # 保留 EXIF（拍摄日期等）
    exif = img.info.get("exif", b"")

    save_kwargs = {"quality": JPEG_QUALITY, "optimize": True}
    if exif:
        save_kwargs["exif"] = exif

    # 转 RGB（防 RGBA/CMYK 等）
    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    img.save(thumb, "JPEG", **save_kwargs)
    new_size_bytes = thumb.stat().st_size

    reduction = (1 - new_size_bytes / orig_size) * 100 if orig_size else 0
    msg = f"  ✅ {src.name} → {thumb.relative_to(src.parent.parent)}: "
    if resized:
        msg += f"{w}x{h} → {img.size[0]}x{img.size[1]}, "
    msg += f"{human_size(orig_size)} → {human_size(new_size_bytes)} ({reduction:+.1f}%)"
    print(msg)

print("\n💡 下一步:")
print("   1. git add images/slider/thumbs/  （新生成的缩略图）")
print("   2. 编辑 _data/slider.yml 顶部加新图条目")
print("   3. git commit + push")
PYEOF
