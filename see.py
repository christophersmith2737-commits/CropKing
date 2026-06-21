#!/usr/bin/env python3
"""Image description tool via Doubao (Volcano Engine) Vision API.
Supports both Responses API (/api/v3/responses) and Chat Completions API.

Usage:
    python see.py <image_path> [--question "specific question"]
    python see.py <image_path1> <image_path2> ...  (multi-image, max 5)

Config: edit ~/.claude/tools/see_config.json
"""

import sys, os, json, base64, argparse
from pathlib import Path
from urllib.request import Request, urlopen, ProxyHandler, build_opener, install_opener
from urllib.error import URLError

CONFIG_FILE = Path.home() / ".claude" / "tools" / "see_config.json"
DEFAULT_CONFIG = {
    "api_key": os.environ.get("DOUBAO_API_KEY", "YOUR_API_KEY_HERE"),
    "model": "doubao-seed-2-0-pro-260215",
    "endpoint": "https://ark.cn-beijing.volces.com/api/v3/responses",
    "endpoint_id": None,
    "max_tokens": 1024,
    "temperature": 0.7,
    "proxy": "http://127.0.0.1:7897",
}

MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = {**DEFAULT_CONFIG, **json.load(f)}
    else:
        cfg = DEFAULT_CONFIG
    if cfg["api_key"] == "YOUR_API_KEY_HERE":
        print("[ERROR] Please configure API Key:")
        print(f"   1. Edit {CONFIG_FILE} and set api_key")
        print(f"   2. Or set env: DOUBAO_API_KEY=your-key")
        sys.exit(1)
    return cfg


def encode_image(path, use_responses_api=True):
    """Encode image to base64 data URL."""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)
    mime = MIME_MAP.get(p.suffix.lower(), "image/png")
    data = base64.b64encode(p.read_bytes()).decode("utf-8")

    if use_responses_api:
        # Responses API format: {"type": "input_image", "image_url": "..."}
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{data}",
        }
    else:
        # Chat Completions API format
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        }


def build_body(cfg, images, prompt):
    """Build request body, auto-detect Responses API vs Chat Completions."""
    is_responses = "/responses" in cfg["endpoint"]

    if is_responses:
        content = []
        for img in images:
            content.append(encode_image(img, use_responses_api=True))
        content.append({"type": "input_text", "text": prompt})
        return {
            "model": cfg["model"],
            "input": [{"role": "user", "content": content}],
        }
    else:
        content = []
        for img in images:
            content.append(encode_image(img, use_responses_api=False))
        content.append({"type": "text", "text": prompt})
        return {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": content}],
            "max_tokens": cfg["max_tokens"],
            "temperature": cfg["temperature"],
        }


def parse_response(data, endpoint):
    """Parse API response, handling both formats."""
    if "/responses" in endpoint:
        # Responses API: output[].content[].text
        for item in data.get("output", []):
            if item.get("type") == "message" and item.get("role") == "assistant":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c["text"]
        return data.get("output", [{}])[0].get("content", [{}])[0].get("text", "")
    else:
        # Chat Completions API
        return data["choices"][0]["message"]["content"]


def call_api(cfg, images, prompt):
    """Call Doubao vision API."""
    if cfg.get("proxy"):
        handler = ProxyHandler({"https": cfg["proxy"], "http": cfg["proxy"]})
        install_opener(build_opener(handler))

    url = cfg["endpoint"]
    if cfg.get("endpoint_id"):
        url = f"https://ark.cn-beijing.volces.com/api/v3/endpoints/{cfg['endpoint_id']}/chat/completions"

    body = build_body(cfg, images, prompt)

    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return parse_response(data, cfg["endpoint"])
    except URLError as e:
        print(f"[ERROR] API request failed: {e}")
        if hasattr(e, "read"):
            print(e.read().decode()[:500])
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Describe images using Doubao Vision")
    parser.add_argument("images", nargs="*", help="Image file path(s)")
    parser.add_argument("--question", "-q", default=None,
                        help="Specific question about the image(s)")
    parser.add_argument("--max-tokens", "-m", type=int, default=None)
    parser.add_argument("--config", "-c", action="store_true",
                        help="Print config and exit")
    args = parser.parse_args()

    if args.config:
        cfg = load_config()
        safe = {k: (v[:8] + "***" if k == "api_key" and v != "YOUR_API_KEY_HERE" else v)
                for k, v in cfg.items()}
        print(json.dumps(safe, indent=2, ensure_ascii=False))
        return

    if not args.images:
        parser.print_help()
        sys.exit(1)

    cfg = load_config()
    if args.max_tokens:
        cfg["max_tokens"] = args.max_tokens

    prompt = args.question or "Please describe the content, style, and key details of this image in Chinese."

    images = args.images
    if len(images) > 5:
        print(f"[WARN] Max 5 images, using first 5 (received {len(images)})")
        images = images[:5]

    result = call_api(cfg, images, prompt)
    # Always write to file to avoid Windows GBK encoding issues
    outfile = Path(os.environ.get("TEMP", "/tmp")) / "see_output.txt"
    outfile.write_text(result, encoding="utf-8")
    # Try stdout, fallback silently
    try:
        print(result)
    except UnicodeEncodeError:
        pass
    print(f"[Output saved to {outfile}]", file=sys.stderr)


if __name__ == "__main__":
    main()
