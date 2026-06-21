# CropKing 裁图王

AI-assisted image cropping tool with multi-provider vision support.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Three manual crop modes**: Circle, fixed-ratio square, free-form rectangle
- **AI face detection**: Auto-locate faces in group photos via vision API, place crop selections automatically
- **Custom shape masks**: Import PNG renders with transparent backgrounds as crop masks
- **Multi-provider**: Supports Doubao (Volcano Engine), OpenAI (GPT-4V), Anthropic (Claude), and OpenAI-compatible APIs
- **Batch save**: Crop multiple selections at once with auto-naming

## Quick Start

```bash
pip install PyQt5 Pillow
python cropking.py [image_path]
```

Drag an image into the window, or use **File → Open**.

## AI Setup

Copy `see_config.example.json` to `see_config.json` and configure your provider:

**Doubao (Volcano Engine)**
```json
{
    "api_key": "ark-xxxxxxxxxxxxx",
    "model": "doubao-seed-2-0-pro-260215",
    "endpoint": "https://ark.cn-beijing.volces.com/api/v3/responses"
}
```

**OpenAI (GPT-4V)**
```json
{
    "api_key": "sk-xxxxxxxxxxxxx",
    "model": "gpt-4o",
    "endpoint": "https://api.openai.com/v1/chat/completions"
}
```

**Anthropic (Claude)**
```json
{
    "api_key": "sk-ant-xxxxxxxxxxxxx",
    "model": "claude-sonnet-4-6",
    "endpoint": "https://api.anthropic.com/v1/messages"
}
```

## Usage

| Action | Key/Mouse |
|---|---|
| Switch crop mode | `1` Circle / `2` Square / `3` Free Rect |
| Draw selection | Left-click drag |
| Move selection | Drag inside selection |
| Resize selection | Drag corner handles (Ctrl+drag for square) |
| AI face detect | `Ctrl+D` or toolbar button |
| Save crop | `Enter` or crop button |
| Batch save all | Batch button |
| Cancel selection | `Esc` |
| Zoom | Mouse wheel |
| Pan | Right/middle drag |

## AI Face Detection

1. Open a group photo
2. Press `Ctrl+D` or click **🤖 AI 识人**
3. The vision model locates each face center, auto-places crop boxes
4. Drag to fine-tune position/size
5. Press `Enter` to batch save all

## Custom Shape Masks

1. Click **蒙版裁剪** in the right panel
2. Import a PNG with transparent background (e.g., character render)
3. Place, scale, and position the mask
4. Save — the output preserves the mask shape with transparency

## File Structure

```
~/.claude/tools/
├── cropking.py           # Main application
├── see.py                # Standalone vision API tool
├── see_config.json       # Your API configuration
├── see_config.example.json  # Configuration template
└── shapes/               # Custom shape library (auto-created)
```

## Requirements

- Python 3.10+
- PyQt5
- Pillow

## Author

Harlemonica

## License

MIT
