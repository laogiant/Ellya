"""
Generate and analyze images using google-genai SDK.
Requires GEMINI_API_KEY in environment.

Usage:
  python scripts/genai_media.py generate -p "a prompt" -i assets/base.png
  python scripts/genai_media.py generate -s style_name
  python scripts/genai_media.py analyze <image_path> [style_name]
  python scripts/genai_media.py series -i assets/base.png [-n 4]
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Load .env from project root (or any parent dir)

from google import genai
from google.genai import types
from PIL import Image

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = ROOT_DIR / "output"
STYLES_DIR = ROOT_DIR / "styles"
ANALYSIS_PROMPT_FILE = ROOT_DIR / "ANALYSIS_PROMPT.md"

DEFAULT_MODEL = "gemini-3-pro-image-preview"
FUSION_MODEL = "gemini-3-flash-preview"
DEFAULT_PROMPT = "A photorealistic portrait of the same person in a natural setting."
IDENTITY_PREFIX = (
    "Based on the reference image, keep the same person and facial identity, "
    "then add or adjust the following details: "
)

SCENE_EXTRACT_PROMPT = (
    "Analyze this image and extract two things:\n"
    "1. SCENE: Describe the environment, lighting, atmosphere, and background in 1-2 sentences.\n"
    "2. CHARACTER: Describe the person's appearance, outfit, hair, and any distinctive features in 1-2 sentences.\n"
    "Reply ONLY with:\nSCENE: <scene description>\nCHARACTER: <character description>"
)

# Prompt to classify scene type and generate context
SCENE_CLASSIFICATION_PROMPT = (
    "Analyze the following scene and character information, determine which shooting type it belongs to, "
    "and generate corresponding content.\n\n"
    "Scene: {scene}\n"
    "Character: {character}\n\n"
    "Reply ONLY with this format (no other content):\n"
    "MODE: [story or pose]\n"
    "CONTEXT: [If story mode: write a brief story plot (1-2 sentences) describing the person's story in this scene. "
    "If pose mode: write a brief scene summary (1-2 sentences) describing the shooting background and atmosphere]"
)

# Prompt to generate pose variations with specific angles and postures
# {count}, {scene}, {character}, {context}, {numbered_list} will be replaced at runtime
POSE_VARIATION_PROMPT = (
    "Based on the following scene and character information, generate {count} different shooting angles and posture parameters "
    "for generating multiple portrait photos.\n\n"
    "Scene: {scene}\n"
    "Character: {character}\n"
    "Scene Summary: {context}\n\n"
    "Each parameter should include:\n"
    "- Camera angle (e.g., front-facing, three-quarter profile, side profile, overhead, low-angle)\n"
    "- Body posture (e.g., standing, seated, leaning against wall)\n"
    "- Facial expression and eye direction\n"
    "- Hand position\n\n"
    "Reply ONLY with this format (no other content):\n"
    "{numbered_list}"
)

# Prompt to generate story-continuation scenes
# {count}, {scene}, {character}, {context}, {numbered_list} will be replaced at runtime
STORY_VARIATION_PROMPT = (
    "Based on the following scene, character, and story plot, generate {count} story-continuation scene descriptions "
    "for generating a photo sequence.\n\n"
    "Scene: {scene}\n"
    "Character: {character}\n"
    "Story Plot: {context}\n\n"
    "Each scene should be a natural extension of the story, showing different moments or actions. "
    "Ensure logical progression and distinct activities.\n\n"
    "Reply ONLY with this format (no other content):\n"
    "{numbered_list}"
)


def get_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Error: GEMINI_API_KEY is missing.")
    return api_key


def build_image_part(image_path: str):
    path = Path(image_path)
    if not path.exists():
        return None

    with Image.open(path) as img:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return types.Part.from_bytes(mime_type="image/png", data=buf.getvalue())


def send_media(channel: str | None, target: str | None, file_path: str | None = None, message: str | None = None) -> None:
    """Send media via OpenClaw (if available).
    
    Note: This function is kept for backward compatibility but should not be called directly.
    Instead, let the skill handler (Ellya) send media according to SKILL.md guidance.
    """
    if not channel or not target:
        return
    if not file_path and not message:
        return

    cmd = ["openclaw", "message", "send", "--channel", channel, "--target", target]
    if file_path:
        cmd += ["--media", file_path]
    if message:
        cmd += ["--message", message]

    try:
        subprocess.run(cmd, check=True)
        print("Sent via OpenClaw.")
    except FileNotFoundError:
        print("Warning: openclaw command not found. Skipping send.")
    except subprocess.CalledProcessError as exc:
        print(f"Warning: Failed to send media via OpenClaw: {exc}")


def extract_first_text(response) -> str:
    if not hasattr(response, "candidates") or not response.candidates:
        return ""

    parts = response.candidates[0].content.parts
    for part in parts:
        if getattr(part, "text", None):
            return part.text.strip()
    return ""


def sanitize_style_name(raw_name: str) -> str:
    lowered = raw_name.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    lowered = re.sub(r"_+", "_", lowered).strip("_")
    return lowered[:60]


def extract_style_name_and_body(text: str) -> tuple[str, str]:
    pattern = re.compile(r"(?im)^\s*Style\s*Name\s*:\s*(.+?)\s*$")
    match = pattern.search(text)
    if not match:
        return "", text.strip()

    generated_name = match.group(1).strip()
    body = pattern.sub("", text, count=1).strip()
    return generated_name, body


def resolve_style_name(manual_name: str | None, generated_name: str) -> str:
    if manual_name:
        manual = sanitize_style_name(manual_name)
        if manual:
            return manual

    generated = sanitize_style_name(generated_name)
    if generated:
        return generated

    return "style_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_unique_style_name(base_name: str) -> str:
    candidate = base_name
    idx = 2
    while (STYLES_DIR / f"{candidate}.md").exists():
        candidate = f"{base_name}_{idx}"
        idx += 1
    return candidate


def save_images_from_response(
    response,
    output_dir: Path | None = None,
    prefix: str = "",
) -> list[str]:
    """Save all inline images from *response* and return their file paths.

    Args:
        output_dir: Directory to write images into. Defaults to OUTPUT_DIR.
        prefix:     Optional filename prefix, e.g. "02_" for series numbering.
    """
    dest = output_dir or OUTPUT_DIR
    files: list[str] = []
    if not hasattr(response, "candidates") or not response.candidates:
        return files

    for part in response.candidates[0].content.parts:
        inline_data = getattr(part, "inline_data", None)
        if not inline_data:
            continue

        filename = f"{prefix}ellya_{os.getpid()}_{len(files)}.png"
        file_path = dest / filename
        with open(file_path, "wb") as f:
            f.write(inline_data.data)
        files.append(str(file_path))
        print(f"Saved image: {file_path}")

    return files


def load_style_prompt(style_name: str) -> str:
    style_file = STYLES_DIR / f"{style_name}.md"
    if not style_file.exists():
        print(f"Style not found, skip: {style_name}")
        return ""

    with open(style_file, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        print(f"Style is empty, skip: {style_name}")
    return content


def fuse_style_prompts(style_prompts: list[str], api_key: str) -> str:
    if not style_prompts:
        return ""
    if len(style_prompts) == 1:
        return style_prompts[0]

    client = genai.Client(api_key=api_key)
    instruction = "Merge these style descriptions into one concise image generation prompt."
    response = client.models.generate_content(
        model=FUSION_MODEL,
        contents=[instruction] + style_prompts,
    )
    return extract_first_text(response)


def resolve_final_prompt(prompt: str | None, styles: list[str] | None, api_key: str) -> str:
    if styles:
        selected = styles[:3]
        loaded = [load_style_prompt(name) for name in selected]
        loaded = [s for s in loaded if s]

        if loaded:
            fused = fuse_style_prompts(loaded, api_key).strip()
            return fused or DEFAULT_PROMPT

        print("No valid style content found. Falling back to default prompt.")
        return DEFAULT_PROMPT

    return (prompt or "").strip() or DEFAULT_PROMPT


def build_generation_prompt(prompt: str) -> str:
    text = (prompt or "").strip() or DEFAULT_PROMPT
    return f"{IDENTITY_PREFIX}{text}"


def extract_scene_and_character(image_part, api_key: str) -> tuple[str, str]:
    """Call AI to extract scene and character descriptions from a base image.

    Returns (scene, character) tuple.
    - scene: environment description
    - character: person description
    """
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=FUSION_MODEL,
            contents=[image_part, SCENE_EXTRACT_PROMPT],
        )
        text = extract_first_text(response)
    except Exception as exc:
        print(f"Scene extraction error: {exc}")
        return "", ""

    scene = ""
    character = ""
    for line in text.splitlines():
        if line.upper().startswith("SCENE:"):
            scene = line[len("SCENE:"):].strip()
        elif line.upper().startswith("CHARACTER:"):
            character = line[len("CHARACTER:"):].strip()

    return scene, character


def classify_scene_and_generate_context(scene: str, character: str, count: int, api_key: str) -> tuple[str, str, list[str]]:
    """由AI自动识别场景类型，并生成相应的上下文内容和变体列表
    
    - 如果识别为story模式：生成简短的故事情节，并生成指定数量的故事延展场景
    - 如果识别为pose模式：生成简短的场景总结，并生成指定数量的不同角度、姿势参数
    
    Args:
        scene: 从图片中提取的场景描述
        character: 从图片中提取的角色描述
        count: 要生成的变体数量
        api_key: Gemini API密钥
        
    Returns:
        (mode, context, variations) 元组
        - mode: "story" 或 "pose"
        - context: 故事情节（story模式）或场景总结（pose模式）
        - variations: 用于生成图片的描述列表（长度为count）
    """
    client = genai.Client(api_key=api_key)
    
    # 第一步：由AI判断场景类型并生成相应内容
    classification_prompt = SCENE_CLASSIFICATION_PROMPT.format(
        scene=scene or "unspecified",
        character=character or "unspecified"
    )
    
    try:
        response = client.models.generate_content(
            model=FUSION_MODEL,
            contents=[classification_prompt],
        )
        text = extract_first_text(response)
    except Exception as exc:
        print(f"Scene classification error: {exc}")
        return "story", "A moment in the scene", ["A photorealistic portrait in the scene"] * count
    
    # 解析AI响应
    mode = "story"
    context = ""
    
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("MODE:"):
            mode_text = line[len("MODE:"):].strip().lower()
            mode = "story" if "story" in mode_text else "pose"
        elif line.upper().startswith("CONTEXT:"):
            context = line[len("CONTEXT:"):].strip()
    
    # 第二步：根据模式生成相应的变体描述
    if mode == "story":
        variations = generate_story_variations(scene, character, context, count, api_key)
    else:
        variations = generate_pose_variations(scene, character, context, count, api_key)
    
    return mode, context, variations


def generate_pose_variations(scene: str, character: str, context: str, count: int, api_key: str) -> list[str]:
    """为pose模式生成指定数量的角度、姿势参数"""
    client = genai.Client(api_key=api_key)
    
    # 生成编号列表
    numbered_list = "\n".join([f"{i}. [Detailed description of angle and posture {i}]" for i in range(1, count + 1)])
    
    # 使用顶部定义的提示词模板
    pose_prompt = POSE_VARIATION_PROMPT.format(
        count=count,
        scene=scene or "unspecified",
        character=character or "unspecified",
        context=context or "unspecified",
        numbered_list=numbered_list
    )
    
    try:
        response = client.models.generate_content(
            model=FUSION_MODEL,
            contents=[pose_prompt],
        )
        text = extract_first_text(response)
    except Exception as exc:
        print(f"Pose variation generation error: {exc}")
        # 返回默认的pose变体，循环填充到count数量
        defaults = [
            "front-facing pose, looking directly at camera, confident expression",
            "three-quarter angle, relaxed natural expression",
            "side profile view, elegant posture",
            "seated pose, relaxed and casual",
            "dynamic standing pose, energetic stance"
        ]
        # 循环填充到count数量
        result = []
        for i in range(count):
            result.append(defaults[i % len(defaults)])
        return result
    
    variations = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(r"^\d+[.)\s]+", "", line).strip()
        if cleaned:
            variations.append(cleaned)
    
    # 确保返回指定数量的变体
    if len(variations) < count:
        # 如果不足，用默认值循环补充
        defaults = [
            "front-facing pose, looking directly at camera, confident expression",
            "three-quarter angle, relaxed natural expression",
            "side profile view, elegant posture",
            "seated pose, relaxed and casual",
            "dynamic standing pose, energetic stance"
        ]
        while len(variations) < count:
            variations.append(defaults[len(variations) % len(defaults)])
    
    return variations[:count]


def generate_story_variations(scene: str, character: str, context: str, count: int, api_key: str) -> list[str]:
    """为story模式生成指定数量的故事延展场景"""
    client = genai.Client(api_key=api_key)
    
    # 生成编号列表
    numbered_list = "\n".join([f"{i}. [Detailed description of story scene {i}]" for i in range(1, count + 1)])
    
    # 使用顶部定义的提示词模板
    story_prompt = STORY_VARIATION_PROMPT.format(
        count=count,
        scene=scene or "unspecified",
        character=character or "unspecified",
        context=context or "unspecified",
        numbered_list=numbered_list
    )
    
    try:
        response = client.models.generate_content(
            model=FUSION_MODEL,
            contents=[story_prompt],
        )
        text = extract_first_text(response)
    except Exception as exc:
        print(f"Story variation generation error: {exc}")
        # 返回默认的story变体，循环填充到count数量
        defaults = [
            "A moment in the scene",
            "Another moment",
            "Continuing the story",
            "Final moment",
            "A new perspective"
        ]
        # 循环填充到count数量
        result = []
        for i in range(count):
            result.append(defaults[i % len(defaults)])
        return result
    
    variations = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(r"^\d+[.)\s]+", "", line).strip()
        if cleaned:
            variations.append(cleaned)
    
    # 确保返回指定数量的变体
    if len(variations) < count:
        # 如果不足，用默认值循环补充
        defaults = [
            "A moment in the scene",
            "Another moment",
            "Continuing the story",
            "Final moment",
            "A new perspective"
        ]
        while len(variations) < count:
            variations.append(defaults[len(variations) % len(defaults)])
    
    return variations[:count]


def do_generate_series(
    input_image: str,
    count: int,
    custom_variations: list[str] | None = None,
) -> None:
    """Generate a series of images based on a single reference image.

    Steps:
      1. Load reference image.
      2. Extract scene and character description via AI.
      3. Classify scene type (story/pose) and generate context + variations.
      4. For each variation, build a prompt and generate an image.
      5. Attach context (story/summary) to generated images.
      6. Save and optionally send via OpenClaw.
    """
    # Validate count parameter
    if count < 1 or count > 10:
        print(f"Error: count must be between 1 and 10, got {count}")
        return
    
    api_key = get_api_key()
    client = genai.Client(api_key=api_key)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # ── 1. Load base image ────────────────────────────────────────────────────
    image_part = build_image_part(input_image)
    if not image_part:
        print(f"Error: reference image not found: {input_image}")
        return
    print(f"Loaded reference image: {input_image}")

    # Create a timestamped subdirectory for this series run
    series_name = "series_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    series_dir = OUTPUT_DIR / series_name
    series_dir.mkdir(parents=True, exist_ok=True)
    print(f"Series output directory: {series_dir}")

    # Copy base image into series dir as "01_base.<ext>"
    base_ext = Path(input_image).suffix or ".png"
    base_dest = series_dir / f"01_base{base_ext}"
    shutil.copy2(input_image, base_dest)
    print(f"Copied base image: {base_dest}")

    # ── 2. Extract scene and character ────────────────────────────────────────
    print("Extracting scene and character from reference image...")
    scene, character = extract_scene_and_character(image_part, api_key)
    if scene:
        print(f"Scene   : {scene}")
    if character:
        print(f"Character: {character}")

    # ── 3. Classify scene type and generate context + variations ──────────────
    print(f"Classifying scene type and generating {count} variation(s)...")
    mode, context, variations = classify_scene_and_generate_context(scene, character, count, api_key)
    print(f"Mode: {mode}")
    print(f"Context: {context}")
    print(f"Generated {len(variations)} variation(s)")

    # Override with custom variations if provided
    if custom_variations:
        print(f"Using {len(custom_variations)} custom variation(s).")
        variations = custom_variations[:count]  # 限制到count数量
        mode = "custom"

    # 确保variations数量不超过count
    variations = variations[:count]

    all_saved: list[str] = []

    # ── 4. Generate images for each variation ────────────────────────────────
    for idx, variation in enumerate(variations):
        seq = idx + 2  # 01 is reserved for base image
        
        # Build generation prompt
        variation_prompt = (
            f"{IDENTITY_PREFIX}"
            f"The person: {character}. "
            f"The setting: {scene}. "
            f"{variation}."
        )
        
        label = "Story scene" if mode == "story" else ("Pose variation" if mode != "custom" else "Custom variation")
        print(f"\n[{idx + 1}/{len(variations)}] {label}: {variation}")

        try:
            response = client.models.generate_content(
                model=DEFAULT_MODEL,
                contents=[image_part, variation_prompt],
            )
        except Exception as exc:
            print(f"Generation error for variation {idx + 1}: {exc}")
            continue

        saved = save_images_from_response(response, output_dir=series_dir, prefix=f"{seq:02d}_")
        if not saved:
            print(f"No image returned for variation {idx + 1}.")
        else:
            all_saved.extend(saved)

    print(f"\nSeries complete. {len(all_saved)} image(s) saved to: {series_dir}")
    
    # Note: Media sending should be handled by the skill handler (Ellya) according to SKILL.md
    # The generated images are available in series_dir for the skill to send


def do_generate(prompt: str, input_images: list[str] | None) -> None:
    api_key = get_api_key()
    client = genai.Client(api_key=api_key)

    OUTPUT_DIR.mkdir(exist_ok=True)

    image_parts = []
    for image_path in input_images or []:
        part = build_image_part(image_path)
        if part:
            image_parts.append(part)
            print(f"Loaded reference image: {image_path}")
        else:
            print(f"Reference image not found, skip: {image_path}")

    if not image_parts:
        print("No valid reference image. Falling back to default prompt.")
        prompt = DEFAULT_PROMPT

    final_prompt = build_generation_prompt(prompt)
    print(f"Final prompt: {final_prompt}")
    print("Calling model API...")

    try:
        response = client.models.generate_content(
            model=DEFAULT_MODEL,
            contents=[*image_parts, final_prompt],
        )
    except Exception as exc:
        print(f"Generation error: {exc}")
        return

    saved_files = save_images_from_response(response)
    if not saved_files:
        print("No image data returned by model.")
    else:
        print(f"Generated {len(saved_files)} image(s).")
        for file_path in saved_files:
            print(f"  - {file_path}")
    
    # Note: Media sending should be handled by the skill handler (Ellya) according to SKILL.md
    # The generated images are available in OUTPUT_DIR for the skill to send


def generate_main() -> None:
    parser = argparse.ArgumentParser(description="Generate selfie image")
    parser.add_argument("-i", "--input-images", action="append", help="Reference image path")
    parser.add_argument("-p", "--prompt", help="Generation prompt")
    parser.add_argument("-s", "--styles", action="append", help="Style names (max 3)")

    args = parser.parse_args()
    api_key = get_api_key()
    final_prompt = resolve_final_prompt(args.prompt, args.styles, api_key)
    do_generate(final_prompt, args.input_images)


def analyze_main() -> None:
    parser = argparse.ArgumentParser(description="Analyze image and store style prompt")
    parser.add_argument("image_path", help="Image path")
    parser.add_argument("style_name", nargs="?", help="Optional style name override")
    args = parser.parse_args()

    api_key = get_api_key()

    if not ANALYSIS_PROMPT_FILE.exists():
        sys.exit(f"Error: analysis prompt file not found: {ANALYSIS_PROMPT_FILE}")

    with open(ANALYSIS_PROMPT_FILE, "r", encoding="utf-8") as f:
        instruction = f.read().strip()

    part = build_image_part(args.image_path)
    if not part:
        sys.exit(f"Error: image not found: {args.image_path}")

    print(f"Analyzing image: {args.image_path}")

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=DEFAULT_MODEL,
            contents=[part, instruction],
        )
        text = extract_first_text(response)
    except Exception as exc:
        print(f"Analyze error: {exc}")
        return

    if not text:
        print("Empty analysis result. Style file will not be saved.")
        return

    generated_name, body = extract_style_name_and_body(text)
    final_name = resolve_style_name(args.style_name, generated_name)
    final_name = ensure_unique_style_name(final_name)
    final_body = (body or text).strip()
    if not final_body:
        print("Empty analysis body. Style file will not be saved.")
        return

    STYLES_DIR.mkdir(exist_ok=True)
    style_file = STYLES_DIR / f"{final_name}.md"
    with open(style_file, "w", encoding="utf-8") as f:
        f.write(final_body)

    print(f"Saved style prompt: {style_file}")
    
    # Note: Notification should be handled by the skill handler (Ellya) according to SKILL.md


def series_main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a series of images from a reference photo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "How it works:\n"
            "1. AI automatically classifies the scene as 'story' or 'pose' mode\n"
            "2. For story mode: generates N story-continuation scenes\n"
            "3. For pose mode: generates N different angles and postures\n"
            "4. Custom variations can override automatic classification"
        ),
    )
    parser.add_argument("-i", "--input-image", required=True, help="Reference image path")
    parser.add_argument("-n", "--count", type=int, default=3,
                        help="Number of images to generate (default 3, min 1, max 10)")
    parser.add_argument("-v", "--variation", action="append", dest="variations", metavar="PROMPT",
                        help="Custom variation prompt (repeatable, overrides automatic classification)")
    args = parser.parse_args()
    do_generate_series(
        args.input_image, args.count, args.variations,
    )


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        sys.argv.pop(1)
        analyze_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        sys.argv.pop(1)
        generate_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "series":
        sys.argv.pop(1)
        series_main()
        return

    generate_main()


if __name__ == "__main__":
    main()
