#!/usr/bin/env python3
"""CropKing v1.2 — 裁图王：AI 辅助图片裁剪工具
Usage:
    python cropking.py [image_path]
"""
import sys, os, json, re, base64
from enum import Enum
from pathlib import Path
from urllib.request import Request, urlopen, ProxyHandler, build_opener, install_opener
from urllib.error import URLError
from PIL import Image, ImageDraw
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsEllipseItem, QGraphicsRectItem,
    QGraphicsItemGroup, QGraphicsSimpleTextItem,
    QToolBar, QAction, QFileDialog, QStatusBar, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QComboBox, QLineEdit, QSpinBox, QPushButton,
    QGroupBox, QMessageBox, QDockWidget, QGraphicsItem, QFrame,
    QProgressDialog,
)
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt5.QtGui import (
    QPixmap, QPen, QColor, QWheelEvent, QMouseEvent, QKeyEvent,
    QPainter, QFont, QBrush,
)

HANDLE_R = 7
MIN_SIZE = 15

# ── AI Prompt Template ──
AI_PROMPT = """这是一张动漫角色合照。请找出图中每一个独立角色的面部中心点（鼻梁位置）和头部大小，以图片宽高百分比(0-100)返回。

严格按以下格式，每个角色一行：
角色描述|X%|Y%|头部占比%
例如：棕色短发戴黄色发卡|45|30|12

说明：
- X和Y：面部中心在图片中的百分比位置
- 头部占比：头部（含发型）大致占图片短边的百分比。近景角色约10-15，远景角色约6-10
- 每个角色只标注一次
- 只返回坐标行，不要其他文字"""


class CropMode(Enum):
    CIRCLE = "圆形"
    SQUARE = "正矩形"
    RECT = "自由矩形"


# ── Shape library (used by MaskDialog popup) ──
SHAPES_DIR = Path.home() / ".claude" / "tools" / "shapes"


def list_shapes():
    if not SHAPES_DIR.exists():
        return []
    return [(p.stem, p) for p in sorted(SHAPES_DIR.glob("*.png"))]


def import_shape(png_path: str, name: str = None):
    img = Image.open(png_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[-1]
    if alpha.getextrema()[1] == 0:
        raise ValueError("PNG 没有可见轮廓（全透明）")
    SHAPES_DIR.mkdir(parents=True, exist_ok=True)
    dest_name = name or Path(png_path).stem
    dest = SHAPES_DIR / f"{dest_name}.png"
    img.save(str(dest))
    return str(dest)


# ═══════════════════════════════════════════════
# CROP OVERLAY — with label
# ═══════════════════════════════════════════════

class CropCircle(QGraphicsEllipseItem):
    def __init__(self, cx, cy, r, label=""):
        super().__init__()
        self._cx = cx
        self._cy = cy
        self._r = max(MIN_SIZE, r)
        self.label = label
        self._sync()
        self._init_style()
        self.setZValue(100)

    def _init_style(self):
        self.setFlag(QGraphicsItem.ItemIsMovable, False)
        self.setAcceptHoverEvents(True)

    @property
    def centerPt(self): return QPointF(self._cx, self._cy)
    @property
    def radius(self): return self._r
    def setRadius(self, r): self._r = max(MIN_SIZE, r); self._sync()
    def moveTo(self, cx, cy): self._cx = cx; self._cy = cy; self._sync()

    def _sync(self):
        r = self._r
        self.setRect(self._cx - r, self._cy - r, r * 2, r * 2)

    def hitRegion(self, pt: QPointF):
        d = ((pt.x() - self._cx)**2 + (pt.y() - self._cy)**2) ** 0.5
        if d < self._r * 0.35: return 'move'
        if abs(d - self._r) < HANDLE_R * 3: return 'resize'
        if d < self._r: return 'move'
        return 'outside'

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        selected = (hasattr(self, '_selected') and self._selected)
        c = QColor(0xFF, 0x69, 0xB4) if not selected else QColor(0x00, 0xFF, 0xAA)
        painter.setPen(QPen(c, 3, Qt.SolidLine if selected else Qt.DashLine))
        painter.setBrush(QColor(c.red(), c.green(), c.blue(), 25))
        painter.drawEllipse(self.rect())
        # handle
        painter.setPen(QPen(c, 2))
        painter.setBrush(QColor(0xFF, 0xFF, 0xFF))
        hx, hy = self._cx + self._r, self._cy
        painter.drawEllipse(QPointF(hx, hy), HANDLE_R, HANDLE_R)
        # center cross
        painter.setPen(QPen(QColor(0xFF, 0xFF, 0xFF, 100), 1))
        painter.drawLine(QPointF(self._cx - 8, self._cy), QPointF(self._cx + 8, self._cy))
        painter.drawLine(QPointF(self._cx, self._cy - 8), QPointF(self._cx, self._cy + 8))
        # label
        if self.label:
            painter.setPen(QColor(0xFF, 0xFF, 0xFF))
            painter.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            painter.drawText(QPointF(self._cx - 40, self._cy - self._r - 12), self.label)


class CropRect(QGraphicsRectItem):
    def __init__(self, rect: QRectF, fixed_square=False, label=""):
        super().__init__(rect)
        self._fixed_square = fixed_square
        self.label = label
        self._init_style()
        self.setZValue(100)

    def _init_style(self):
        self.setFlag(QGraphicsItem.ItemIsMovable, False)
        self.setAcceptHoverEvents(True)

    def hitRegion(self, pt: QPointF):
        r = self.rect()
        corners = {'tl': r.topLeft(), 'tr': r.topRight(),
                   'bl': r.bottomLeft(), 'br': r.bottomRight()}
        for name, cp in corners.items():
            if ((pt.x() - cp.x())**2 + (pt.y() - cp.y())**2) ** 0.5 < HANDLE_R * 3:
                return name
        if r.contains(pt): return 'move'
        return 'outside'

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        selected = (hasattr(self, '_selected') and self._selected)
        c = QColor(0xFF, 0x69, 0xB4) if not selected else QColor(0x00, 0xFF, 0xAA)
        painter.setPen(QPen(c, 3, Qt.SolidLine if selected else Qt.DashLine))
        painter.setBrush(QColor(c.red(), c.green(), c.blue(), 20))
        painter.drawRect(self.rect())
        painter.setPen(QPen(c, 2))
        painter.setBrush(QColor(0xFF, 0xFF, 0xFF))
        for corner in [self.rect().topLeft(), self.rect().topRight(),
                       self.rect().bottomLeft(), self.rect().bottomRight()]:
            painter.drawEllipse(corner, HANDLE_R, HANDLE_R)
        # Grid
        painter.setPen(QPen(QColor(0xFF, 0xFF, 0xFF, 50), 0.5))
        r = self.rect()
        painter.drawLine(QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.bottom()))
        painter.drawLine(QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y()))
        # Label
        if self.label:
            painter.setPen(QColor(0xFF, 0xFF, 0xFF))
            painter.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            painter.drawText(QPointF(r.left(), r.top() - 10), self.label)


# ═══════════════════════════════════════════════
# IMAGE VIEW
# ═══════════════════════════════════════════════

class ImageView(QGraphicsView):
    zoomChanged = pyqtSignal(float)
    cropRequested = pyqtSignal()
    cancelRequested = pyqtSignal()
    batchCropRequested = pyqtSignal()

    def __init__(self, scene, owner):
        super().__init__(scene)
        self._zoom = 1.0
        self._owner = owner
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCacheMode(QGraphicsView.CacheBackground)
        self.setOptimizationFlag(QGraphicsView.DontSavePainterState, True)

    def wheelEvent(self, e: QWheelEvent):
        f = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
        z = self._zoom * f
        if 0.03 <= z <= 25.0:
            self._zoom = z; self.scale(f, f); self.zoomChanged.emit(z)

    def resetZoom(self):
        self.resetTransform(); self._zoom = 1.0; self.zoomChanged.emit(1.0)

    def fitImage(self, rect: QRectF):
        self.fitInView(rect, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11(); self.zoomChanged.emit(self._zoom)

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() in (Qt.MiddleButton, Qt.RightButton):
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            return super().mousePressEvent(e)
        if e.button() == Qt.LeftButton:
            self.setDragMode(QGraphicsView.NoDrag)
            self._owner.on_view_press(self.mapToScene(e.pos()), e.modifiers())
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._owner.drawing or self._owner.dragging:
            self._owner.on_view_move(self.mapToScene(e.pos()), e.modifiers())
            return
        item = self._owner.active_item
        if item:
            pt = self.mapToScene(e.pos())
            region = item.hitRegion(pt)
            cursors = {'move': Qt.OpenHandCursor, 'tl': Qt.SizeFDiagCursor,
                       'tr': Qt.SizeBDiagCursor, 'bl': Qt.SizeBDiagCursor,
                       'br': Qt.SizeFDiagCursor, 'resize': Qt.CrossCursor}
            self.setCursor(cursors.get(region, Qt.ArrowCursor))
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent):
        if e.button() == Qt.LeftButton and (self._owner.drawing or self._owner.dragging):
            self._owner.on_view_release()
            return
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._owner.crop_items:
                self.batchCropRequested.emit()
            else:
                self.cropRequested.emit()
        elif e.key() in (Qt.Key_Escape, Qt.Key_Delete):
            self.cancelRequested.emit()
        else:
            super().keyPressEvent(e)


# ═══════════════════════════════════════════════
# AI API CALL
# ═══════════════════════════════════════════════

def call_ai_vision(image_path: str, prompt: str) -> str:
    """Call vision API — supports Doubao / OpenAI / Anthropic. Auto-detects format from endpoint URL."""
    config_path = Path.home() / ".claude" / "tools" / "see_config.json"
    cfg = {}
    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))

    api_key = cfg.get("api_key", os.environ.get("DOUBAO_API_KEY", os.environ.get("OPENAI_API_KEY", "")))
    endpoint = cfg.get("endpoint", "https://ark.cn-beijing.volces.com/api/v3/responses")
    model = cfg.get("model", "doubao-seed-2-0-pro-260215")
    proxy_url = cfg.get("proxy", "")

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        raise RuntimeError("请先在 see_config.json 中配置 api_key 和 endpoint\n"
                           "支持 OpenAI / Anthropic / Doubao 等视觉模型")

    if proxy_url:
        handler = ProxyHandler({"https": proxy_url, "http": proxy_url})
        install_opener(build_opener(handler))

    # Encode image
    p = Path(image_path)
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    mime = mime_map.get(p.suffix.lower(), "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode()
    data_url = f"data:{mime};base64,{b64}"

    # Auto-detect API format from endpoint
    if "/responses" in endpoint:
        # ── Doubao Responses API ──
        body = {
            "model": model,
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": data_url},
                {"type": "input_text", "text": prompt},
            ]}],
        }
    elif "anthropic" in endpoint or "/v1/messages" in endpoint:
        # ── Anthropic Messages API ──
        body = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        }
    else:
        # ── OpenAI-compatible (GPT-4V / DeepSeek / Qwen / Doubao chat) ──
        body = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]}],
        }

    headers = {"Content-Type": "application/json"}
    if "anthropic" in endpoint:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(body).encode("utf-8")
    req = Request(endpoint, data=data, headers=headers)

    try:
        with urlopen(req, timeout=60) as resp:
            rdata = json.loads(resp.read())
    except URLError as e:
        raise RuntimeError(f"API 请求失败: {e}")

    # Parse response
    if "/responses" in endpoint:
        for item in rdata.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c["text"]
        return ""
    elif "anthropic" in endpoint or "/v1/messages" in endpoint:
        return rdata["content"][0]["text"]
    else:
        return rdata["choices"][0]["message"]["content"]


def parse_face_coords(text: str, img_w: int, img_h: int) -> list:
    """Parse AI response like '棕色短发|45%|30%|12' into pixel coords + head size."""
    results = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            label = parts[0].strip()
            x_str = parts[1].strip().replace("%", "").strip()
            y_str = parts[2].strip().replace("%", "").strip()
            h_str = parts[3].strip().replace("%", "").strip() if len(parts) >= 4 else "12"
            try:
                x_pct = float(x_str) / 100.0
                y_pct = float(y_str) / 100.0
                h_pct = float(h_str) / 100.0
                px = int(x_pct * img_w)
                py = int(y_pct * img_h)
                # head size: percentage of image min dimension
                head_px = int(h_pct * min(img_w, img_h))
                results.append((label, px, py, head_px))
            except ValueError:
                continue
    return results


# ═══════════════════════════════════════════════
# MASK DIALOG — standalone popup for custom shape cropping
# ═══════════════════════════════════════════════

class MaskDialog(QMainWindow):
    """Standalone dialog for custom shape (PNG mask) cropping."""
    def __init__(self, src_img: Image.Image, src_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("蒙版裁剪 — 自定义形状")
        self.setMinimumSize(1000, 650)
        self.setWindowModality(Qt.ApplicationModal)

        self.src_img = src_img
        self.src_path = src_path
        self._shape_img: Image.Image = None
        self._shape_path: str = None
        self._overlay: QGraphicsRectItem = None
        self._shape_pixmap: QGraphicsPixmapItem = None

        # Main layout
        central = QWidget()
        self.setCentralWidget(central)
        hbox = QHBoxLayout(central)
        hbox.setContentsMargins(0, 0, 0, 0)

        # Left: image view
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setStyleSheet("background: #1e1e2e; border: none;")
        self.view.setMouseTracking(True)
        hbox.addWidget(self.view, 3)

        # Show source image
        tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "__mask_src.png")
        src_img.convert("RGBA").save(tmp, "PNG")
        self._src_pixmap = QPixmap(tmp)
        self.scene.addPixmap(self._src_pixmap)
        self.scene.setSceneRect(QRectF(self._src_pixmap.rect()))
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
        self.view.wheelEvent = self._view_wheel

        # Hooks for mouse
        self.view.mousePressEvent = self._mouse_press
        self.view.mouseMoveEvent = self._mouse_move
        self.view.mouseReleaseEvent = self._mouse_release
        self.view.keyPressEvent = self._key_press
        self._placing = False
        self._dragging = False
        self._drag_start = None
        self._overlay_start_rect = None

        # Right panel
        panel = QWidget()
        panel.setFixedWidth(220)
        panel.setStyleSheet("background: #1e1e2a;")
        plo = QVBoxLayout(panel)
        plo.setSpacing(10)
        plo.setContentsMargins(10, 10, 10, 10)

        g = QGroupBox("形状选择")
        gl = QVBoxLayout(g)
        self.shape_combo = QComboBox()
        self.shape_combo.currentIndexChanged.connect(self._on_shape_changed)
        gl.addWidget(QLabel("选择蒙版形状："))
        gl.addWidget(self.shape_combo)
        btn_row = QHBoxLayout()
        import_btn = QPushButton("+ 导入 PNG")
        import_btn.clicked.connect(self._import_shape)
        btn_row.addWidget(import_btn)
        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedWidth(36)
        refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(refresh_btn)
        gl.addLayout(btn_row)
        plo.addWidget(g)

        self._preview_lbl = QLabel("选择一个形状后在图片上\n点击放置，拖拽缩放\nEnter 保存裁剪")
        self._preview_lbl.setAlignment(Qt.AlignCenter)
        self._preview_lbl.setStyleSheet("color: #6c7086; font-size: 11px;")
        plo.addWidget(self._preview_lbl)

        # Scale slider
        plo.addWidget(QLabel("缩放比例："))
        from PyQt5.QtWidgets import QSlider
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(20, 200)
        self.scale_slider.setValue(100)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)
        plo.addWidget(self.scale_slider)
        self._scale_label = QLabel("100%")
        self._scale_label.setAlignment(Qt.AlignCenter)
        plo.addWidget(self._scale_label)

        crop_btn = QPushButton("✂  保存裁剪")
        crop_btn.setStyleSheet(
            "QPushButton { background: #ff69b4; color: #fff; padding: 8px; "
            "border-radius: 6px; font-weight: bold; }"
            "QPushButton:hover { background: #ff85ca; }")
        crop_btn.clicked.connect(self._do_crop)
        plo.addWidget(crop_btn)

        plo.addStretch()
        hbox.addWidget(panel)

        self._refresh()

    def _view_wheel(self, e):
        f = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
        self.view.scale(f, f)

    def _refresh(self):
        self.shape_combo.clear()
        shapes = list_shapes()
        if shapes:
            for name, _ in shapes:
                self.shape_combo.addItem(name)
            self.shape_combo.setCurrentIndex(0)
            self._on_shape_changed(0)
        else:
            self.shape_combo.addItem("（无形状，请导入PNG）")
            self.shape_combo.setEnabled(False)

    def _on_shape_changed(self, idx):
        shapes = list_shapes()
        if not shapes or idx < 0 or idx >= len(shapes):
            return
        self._shape_path = str(shapes[idx][1])
        self._shape_img = Image.open(self._shape_path).convert("RGBA")
        self._preview_lbl.setText(f"已选中：{shapes[idx][0]}")

    def _import_shape(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 PNG 形状（带透明背景）", "",
            "PNG 图片 (*.png);;所有文件 (*)")
        if not path:
            return
        from PyQt5.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "命名形状", "形状名称：", text=Path(path).stem)
        if not ok or not name:
            return
        try:
            import_shape(path, name)
            self._refresh()
            idx = self.shape_combo.findText(name)
            if idx >= 0:
                self.shape_combo.setCurrentIndex(idx)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _place_overlay(self, scene_pt: QPointF):
        if not self._shape_img:
            return
        # Remove old overlay
        self._remove_overlay()

        iw, ih = self._shape_img.size
        base = 150 * (self.scale_slider.value() / 100.0)
        scale = base / max(iw, ih)
        nw, nh = int(iw * scale), int(ih * scale)

        # Draw bounding rect
        x, y = scene_pt.x() - nw/2, scene_pt.y() - nh/2
        pen = QPen(QColor(0xFF, 0x69, 0xB4), 2.5, Qt.DashLine)
        pen.setCosmetic(True)
        self._overlay = QGraphicsRectItem(x, y, nw, nh)
        self._overlay.setPen(pen)
        self._overlay.setBrush(QColor(0xFF, 0x69, 0xB4, 30))
        self.scene.addItem(self._overlay)

        # Show scaled shape preview
        shape_scaled = self._shape_img.resize((nw, nh), Image.LANCZOS)
        tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "__mask_shape.png")
        shape_scaled.save(tmp, "PNG")
        self._shape_pixmap = self.scene.addPixmap(QPixmap(tmp))
        self._shape_pixmap.setPos(x, y)
        self._shape_pixmap.setOpacity(0.5)
        self._shape_pixmap.setZValue(50)

    def _remove_overlay(self):
        if self._overlay:
            self.scene.removeItem(self._overlay)
            self._overlay = None
        if self._shape_pixmap:
            self.scene.removeItem(self._shape_pixmap)
            self._shape_pixmap = None

    def _on_scale_changed(self, v):
        self._scale_label.setText(f"{v}%")
        if self._overlay:
            r = self._overlay.rect()
            cp = r.center()
            self._remove_overlay()
            iw, ih = self._shape_img.size
            base = 150 * (v / 100.0)
            scale = base / max(iw, ih)
            nw, nh = int(iw * scale), int(ih * scale)
            x, y = cp.x() - nw/2, cp.y() - nh/2
            pen = QPen(QColor(0xFF, 0x69, 0xB4), 2.5, Qt.DashLine)
            pen.setCosmetic(True)
            self._overlay = QGraphicsRectItem(x, y, nw, nh)
            self._overlay.setPen(pen)
            self._overlay.setBrush(QColor(0xFF, 0x69, 0xB4, 30))
            self.scene.addItem(self._overlay)
            shape_scaled = self._shape_img.resize((nw, nh), Image.LANCZOS)
            tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "__mask_shape.png")
            shape_scaled.save(tmp, "PNG")
            self._shape_pixmap = self.scene.addPixmap(QPixmap(tmp))
            self._shape_pixmap.setPos(x, y)
            self._shape_pixmap.setOpacity(0.5)
            self._shape_pixmap.setZValue(50)

    def _mouse_press(self, e):
        if e.button() != Qt.LeftButton:
            self.view.setDragMode(QGraphicsView.ScrollHandDrag)
            return QGraphicsView.mousePressEvent(self.view, e)

        if not self._shape_img:
            return

        pt = self.view.mapToScene(e.pos())
        # Check if clicking on existing overlay
        if self._overlay and self._overlay.rect().contains(pt):
            self._dragging = True
            self._drag_start = pt
            self._overlay_start_rect = QRectF(self._overlay.rect())
            return

        # Place new overlay
        self._place_overlay(pt)

    def _mouse_move(self, e):
        if self._dragging and self._overlay_start_rect:
            pt = self.view.mapToScene(e.pos())
            dx = pt.x() - self._drag_start.x()
            dy = pt.y() - self._drag_start.y()
            r = self._overlay_start_rect.translated(dx, dy)
            self._overlay.setRect(r)
            if self._shape_pixmap:
                self._shape_pixmap.setPos(r.topLeft())
            return
        QGraphicsView.mouseMoveEvent(self.view, e)

    def _mouse_release(self, e):
        self._dragging = False
        self._overlay_start_rect = None

    def _key_press(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._do_crop()
        elif e.key() == Qt.Key_Escape:
            self._remove_overlay()

    def _do_crop(self):
        if not self._overlay or not self._shape_img:
            QMessageBox.information(self, "提示", "请先在图片上点击放置形状。")
            return

        r = self._overlay.rect()
        x1, y1 = int(r.left()), int(r.top())
        x2, y2 = int(r.right()), int(r.bottom())
        x1, x2 = sorted([max(0, x1), min(self.src_img.width, x2)])
        y1, y2 = sorted([max(0, y1), min(self.src_img.height, y2)])

        if x2 - x1 < 5 or y2 - y1 < 5:
            QMessageBox.warning(self, "警告", "选区太小。")
            return

        # Scale shape to overlay size
        shape_scaled = self._shape_img.resize((x2 - x1, y2 - y1), Image.LANCZOS)
        canvas = Image.new("RGBA", self.src_img.size, (0, 0, 0, 0))
        canvas.paste(shape_scaled, (x1, y1), shape_scaled)
        result = self.src_img.copy().convert("RGBA")
        mask = canvas.split()[-1]
        result.putalpha(mask)
        bbox = mask.getbbox()
        if bbox:
            result = result.crop(bbox)

        # Auto-name
        base = Path(self.src_path).stem
        shape_name = Path(self._shape_path).stem if self._shape_path else "mask"
        out = Path(self.src_path).parent / f"{base}_{shape_name}.png"
        result.save(str(out))
        QMessageBox.information(self, "完成", f"已保存：{out.name}")
        self._remove_overlay()

    def closeEvent(self, e):
        self._remove_overlay()
        super().closeEvent(e)


# ═══════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════

class CropKing(QMainWindow):
    def __init__(self, image_path=None):
        super().__init__()
        self.setWindowTitle("裁图王 v1.2")
        self.setMinimumSize(1280, 780)

        self.mode = CropMode.SQUARE
        self.src_path = None
        self.src_img: Image.Image = None
        self.pixmap: QPixmap = None
        self.pixmap_item: QGraphicsPixmapItem = None
        self.crop_items: list = []  # list of CropCircle/CropRect
        self._active_idx = -1  # index of selected item
        self.drawing = False
        self.dragging = False
        self.start_pt: QPointF = None
        self.drag_region = None
        self.counter = 0
        self._dirty = False
        self._drag_start_cx = self._drag_start_cy = self._drag_start_r = 0
        self._drag_start_rect: QRectF = None

        self.scene = QGraphicsScene(self)
        self.view = ImageView(self.scene, self)
        self.view.zoomChanged.connect(self._on_zoom)
        self.view.cropRequested.connect(self._do_crop)
        self.view.batchCropRequested.connect(self._do_batch_crop)
        self.view.cancelRequested.connect(self._cancel)
        self.setCentralWidget(self.view)
        self.setAcceptDrops(True)

        self._build_dock()
        self._build_toolbar()
        self._build_statusbar()

        if image_path and os.path.exists(image_path):
            self.load_image(image_path)

    # ── active item ──
    @property
    def active_item(self):
        if 0 <= self._active_idx < len(self.crop_items):
            return self.crop_items[self._active_idx]
        return None

    def _select_item(self, idx):
        # Deselect all
        for item in self.crop_items:
            item._selected = False
            item.update()
        self._active_idx = idx
        if idx >= 0 and idx < len(self.crop_items):
            self.crop_items[idx]._selected = True
            self.crop_items[idx].update()
            self._dirty = True
            self._update_crop_btn()

    # ── DOCK ──
    def _build_dock(self):
        d = QDockWidget("设置", self)
        d.setFeatures(QDockWidget.NoDockWidgetFeatures)
        d.setFixedWidth(230)
        w = QWidget()
        w.setStyleSheet("background: #1e1e2a;")
        lo = QVBoxLayout(w); lo.setSpacing(14); lo.setContentsMargins(12, 12, 12, 12)

        g1 = QGroupBox("输出格式")
        g1l = QVBoxLayout(g1)
        self.fmt_combo = QComboBox(); self.fmt_combo.addItems(["PNG", "JPEG"])
        g1l.addWidget(self.fmt_combo); lo.addWidget(g1)

        g2 = QGroupBox("命名规则")
        g2l = QVBoxLayout(g2)
        g2l.addWidget(QLabel("名称后缀："))
        self.prefix_edit = QLineEdit(); self.prefix_edit.setPlaceholderText("例如：_crop")
        g2l.addWidget(self.prefix_edit)
        g2l.addWidget(QLabel("起始序号："))
        self.start_spin = QSpinBox(); self.start_spin.setRange(0, 9999); self.start_spin.setValue(0)
        g2l.addWidget(self.start_spin)
        lo.addWidget(g2)

        g3 = QGroupBox("AI 头部范围")
        g3l = QVBoxLayout(g3)
        self.head_mode_combo = QComboBox()
        self.head_mode_combo.addItems(["面部（紧凑）", "全头（含发型）"])
        self.head_mode_combo.setCurrentIndex(1)
        g3l.addWidget(self.head_mode_combo)
        lo.addWidget(g3)

        g4 = QGroupBox("蒙版裁剪")
        g4l = QVBoxLayout(g4)
        g4l.addWidget(QLabel("用自定义PNG形状（立绘轮廓等）进行蒙版裁剪"))
        open_mask_btn = QPushButton("🎭 打开蒙版裁剪")
        open_mask_btn.setStyleSheet(
            "QPushButton { background: #8a2be2; color: #fff; padding: 8px; "
            "border-radius: 6px; font-weight: bold; }"
            "QPushButton:hover { background: #a04eef; }")
        open_mask_btn.clicked.connect(self._open_mask_dialog)
        g4l.addWidget(open_mask_btn)
        lo.addWidget(g4)

        self.crop_btn = QPushButton("✂  裁剪并保存")
        self.crop_btn.clicked.connect(self._do_crop)
        self._update_crop_btn(); lo.addWidget(self.crop_btn)

        self.batch_btn = QPushButton("📦 批量保存全部")
        self.batch_btn.clicked.connect(self._do_batch_crop)
        self._update_batch_btn(); lo.addWidget(self.batch_btn)

        self.counter_lbl = QLabel("下一个：—"); self.counter_lbl.setWordWrap(True)
        lo.addWidget(self.counter_lbl)

        rst = QPushButton("↺ 重置计数"); rst.clicked.connect(lambda: self._reset_counter())
        lo.addWidget(rst)

        self.zoom_lbl = QLabel("缩放：100%"); lo.addWidget(self.zoom_lbl)

        hint = QLabel(
            "🖱 点击选区=选中(绿框)\n   再拖拽内部=移动\n   拖拽四角=调大小\n"
            "   点击空白=新建选区\n🖱 滚轮缩放 | 右键平移\n"
            "⌨ Enter=批量保存\n   Esc=清除当前选区\n"
            "   1/2/3=切换裁剪模式"
        )
        hint.setStyleSheet("color: #6c7086; font-size: 11px;"); lo.addWidget(hint)
        lo.addStretch()
        d.setWidget(w); self.addDockWidget(Qt.RightDockWidgetArea, d)

    def _update_crop_btn(self):
        if self._dirty and self.active_item:
            self.crop_btn.setText("✂  保存当前选区")
            self.crop_btn.setEnabled(True)
            self.crop_btn.setStyleSheet(
                "QPushButton { background: #ff69b4; color: #fff; padding: 8px 12px; "
                "border-radius: 6px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background: #ff85ca; }")
        elif not self.crop_items and not self._dirty:
            self.crop_btn.setText("✂  裁剪并保存")
            self.crop_btn.setEnabled(False)
            self.crop_btn.setStyleSheet(
                "QPushButton { background: #313244; color: #6c7086; padding: 8px 12px; "
                "border-radius: 6px; font-weight: bold; font-size: 12px; }")
        else:
            self.crop_btn.setText("✂  保存当前选区")
            self.crop_btn.setEnabled(True)
            self.crop_btn.setStyleSheet(
                "QPushButton { background: #ff69b4; color: #fff; padding: 8px 12px; "
                "border-radius: 6px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background: #ff85ca; }")

    def _update_batch_btn(self):
        n = len(self.crop_items)
        if n > 1:
            self.batch_btn.setText(f"📦 批量保存 ({n}个)")
            self.batch_btn.setEnabled(True)
            self.batch_btn.setStyleSheet(
                "QPushButton { background: #8a2be2; color: #fff; padding: 8px 12px; "
                "border-radius: 6px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background: #a04eef; }")
        else:
            self.batch_btn.setText("📦 批量保存全部")
            self.batch_btn.setEnabled(False)
            self.batch_btn.setStyleSheet(
                "QPushButton { background: #313244; color: #6c7086; padding: 8px 12px; "
                "border-radius: 6px; font-weight: bold; font-size: 12px; }")

    def _mark_dirty(self):
        if not self._dirty:
            self._dirty = True
        self._update_crop_btn()
        self._update_batch_btn()

    def _open_mask_dialog(self):
        if not self.src_img:
            QMessageBox.information(self, "提示", "请先打开一张图片。")
            return
        dlg = MaskDialog(self.src_img, self.src_path, self)
        dlg.show()

    def _reset_counter(self):
        self.counter = self.start_spin.value(); self._refresh_counter()

    def _refresh_counter(self):
        if not self.src_path:
            self.counter_lbl.setText("下一个：—"); return
        fmt = self.fmt_combo.currentText()
        ext = "jpg" if fmt == "JPEG" else "png"
        prefix = self.prefix_edit.text().strip()
        base = Path(self.src_path).stem
        name = f"{base}{prefix}_{self.counter:02d}.{ext}"
        self.counter_lbl.setText(f"下一个：{name}")

    def _on_zoom(self, z): self.zoom_lbl.setText(f"缩放：{int(z*100)}%")

    # ── TOOLBAR ──
    def _build_toolbar(self):
        tb = QToolBar("模式"); tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        self.act_circle = QAction("⭕ 圆形 [1]", self); self.act_circle.setCheckable(True)
        self.act_circle.triggered.connect(lambda: self._set_mode(CropMode.CIRCLE))
        self.act_circle.setShortcut("1"); tb.addAction(self.act_circle)

        self.act_square = QAction("⬜ 正矩形 [2]", self); self.act_square.setCheckable(True); self.act_square.setChecked(True)
        self.act_square.triggered.connect(lambda: self._set_mode(CropMode.SQUARE))
        self.act_square.setShortcut("2"); tb.addAction(self.act_square)

        self.act_rect = QAction("🔲 自由矩形 [3]", self); self.act_rect.setCheckable(True)
        self.act_rect.triggered.connect(lambda: self._set_mode(CropMode.RECT))
        self.act_rect.setShortcut("3"); tb.addAction(self.act_rect)

        tb.addSeparator()

        self.act_ai = QAction("🤖 AI 识人", self)
        self.act_ai.triggered.connect(self._ai_detect)
        self.act_ai.setShortcut("Ctrl+D")
        tb.addAction(self.act_ai)

        tb.addSeparator()

        a_open = QAction("📂 打开 [Ctrl+O]", self); a_open.triggered.connect(self._open)
        a_open.setShortcut("Ctrl+O"); tb.addAction(a_open)
        a_fit = QAction("🔍 适应 [Ctrl+0]", self)
        a_fit.triggered.connect(lambda: self.view.fitImage(QRectF(self.pixmap.rect())) if self.pixmap else None)
        a_fit.setShortcut("Ctrl+0"); tb.addAction(a_fit)
        a_1 = QAction("1:1 [Ctrl+1]", self); a_1.triggered.connect(lambda: self.view.resetZoom())
        a_1.setShortcut("Ctrl+1"); tb.addAction(a_1)

        tb.addSeparator()
        a_about = QAction("ℹ 关于", self); a_about.triggered.connect(self._show_about)
        tb.addAction(a_about)

    def _set_mode(self, m: CropMode):
        self.mode = m
        self.act_circle.setChecked(m == CropMode.CIRCLE)
        self.act_square.setChecked(m == CropMode.SQUARE)
        self.act_rect.setChecked(m == CropMode.RECT)
        self.status_msg(f"模式：{m.value}")

    # ── STATUSBAR ──
    def _build_statusbar(self):
        self.status = QStatusBar(); self.setStatusBar(self.status)
        self.status_msg("拖入图片或点击「打开」开始")

    def _show_about(self):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser
        dlg = QDialog(self)
        dlg.setWindowTitle("关于 CropKing 裁图王")
        dlg.setMinimumSize(460, 420)
        dlg.setStyleSheet("QDialog { background: #1a1a2e; }")
        lo = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setStyleSheet("QTextBrowser { background: #1a1a2e; color: #ccd; border: none; font-size: 14px; }")
        tb.setHtml("""
            <div style="text-align:center;">
            <h1 style="color:#ff69b4;margin-bottom:4px;">CropKing 裁图王</h1>
            <p style="color:#889;font-size:13px;">v1.2</p>
            <p style="font-size:16px;margin-top:12px;"><b>作者：</b>Harlemonica</p>
            <hr style="border-color:#2a2a40;margin:16px 0;">
            </div>
            <table style="width:100%;line-height:1.9;font-size:13px;">
            <tr><td style="color:#ff69b4;font-weight:bold;width:100px;">🖱 手动裁剪</td>
            <td>圆形 · 正矩形 · 自由矩形<br>选区可拖拽移动、缩放，Enter 保存</td></tr>
            <tr><td style="color:#ff69b4;font-weight:bold;">🤖 AI 识人</td>
            <td>Ctrl+D 调用视觉模型<br>自动识别面部，批量铺选区</td></tr>
            <tr><td style="color:#ff69b4;font-weight:bold;">🎭 蒙版裁剪</td>
            <td>导入透明底 PNG<br>以立绘轮廓为裁剪框，等比缩放</td></tr>
            <tr><td style="color:#ff69b4;font-weight:bold;">🔌 多模型</td>
            <td>豆包 · GPT-4V · Claude<br>OpenAI 兼容 API 自动适配</td></tr>
            </table>
        """)
        lo.addWidget(tb)
        dlg.exec_()

    def status_msg(self, msg): self.status.showMessage(msg)

    # ── FILE ──
    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开图片", "",
                                               "图片文件 (*.jpg *.jpeg *.png);;所有文件 (*)")
        if path: self.load_image(path)

    def load_image(self, path):
        try:
            self.src_img = Image.open(path); self.src_path = path
            self.scene.clear(); self.pixmap_item = None
            self.crop_items.clear(); self._active_idx = -1

            tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "__ck_temp.png")
            self.src_img.convert("RGBA").save(tmp, "PNG")
            self.pixmap = QPixmap(tmp)
            self.pixmap_item = self.scene.addPixmap(self.pixmap)
            self.scene.setSceneRect(QRectF(self.pixmap.rect()))
            self.view.fitImage(QRectF(self.pixmap.rect()))

            self.counter = self.start_spin.value()
            self._dirty = False; self._update_crop_btn(); self._update_batch_btn()
            self._refresh_counter()
            self.setWindowTitle(f"裁图王 — {os.path.basename(path)}  [{self.src_img.width}x{self.src_img.height}]")
            self.status_msg(f"已加载 {os.path.basename(path)}  |  {self.src_img.width}×{self.src_img.height}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法加载图片：\n{e}")

    # ── OVERLAY ──
    def _remove_active(self):
        if self.active_item and self.active_item.scene():
            self.scene.removeItem(self.active_item)
        if 0 <= self._active_idx < len(self.crop_items):
            self.crop_items.pop(self._active_idx)
        self._active_idx = -1
        self._dirty = bool(self.crop_items)
        self._update_crop_btn(); self._update_batch_btn()

    def _clear_all(self):
        for item in self.crop_items:
            if item.scene():
                self.scene.removeItem(item)
        self.crop_items.clear(); self._active_idx = -1
        self._dirty = False
        self._update_crop_btn(); self._update_batch_btn()

    def _to_pixel(self, pt: QPointF):
        if not self.pixmap_item: return 0, 0
        local = self.pixmap_item.mapFromScene(pt)
        return int(local.x()), int(local.y())

    # ── MOUSE ──
    def on_view_press(self, pt: QPointF, mods):
        if not self.pixmap_item: return

        # Check if clicking on existing item
        for idx, item in enumerate(self.crop_items):
            region = item.hitRegion(pt)
            if region != 'outside':
                self._select_item(idx)
                self.dragging = True; self.drag_region = region; self.start_pt = pt
                if isinstance(item, CropCircle):
                    self._drag_start_cx = item.centerPt.x()
                    self._drag_start_cy = item.centerPt.y()
                    self._drag_start_r = item.radius
                else:
                    self._drag_start_rect = QRectF(item.rect())
                return

        # Clicked empty area → deselect
        self._select_item(-1)

        # Start new selection
        self.drawing = True; self.start_pt = pt

        if self.mode == CropMode.CIRCLE:
            item = CropCircle(pt.x(), pt.y(), 60)
        elif self.mode in (CropMode.SQUARE, CropMode.RECT):
            s = 120
            r = QRectF(pt.x() - s/2, pt.y() - s/2, s, s)
            item = CropRect(r, fixed_square=(self.mode == CropMode.SQUARE))
        else:
            return

        self.scene.addItem(item)
        self.crop_items.append(item)
        self._select_item(len(self.crop_items) - 1)

    def on_view_move(self, pt: QPointF, mods):
        item = self.active_item
        if not item: return

        if self.dragging:
            self._mark_dirty()
            dx = pt.x() - self.start_pt.x(); dy = pt.y() - self.start_pt.y()

            if isinstance(item, CropCircle):
                if self.drag_region == 'move':
                    item.moveTo(self._drag_start_cx + dx, self._drag_start_cy + dy)
                elif self.drag_region == 'resize':
                    cx, cy = item.centerPt.x(), item.centerPt.y()
                    r = ((pt.x() - cx)**2 + (pt.y() - cy)**2) ** 0.5
                    item.setRadius(r)
            else:
                if self.drag_region == 'move':
                    r = QRectF(self._drag_start_rect); r.translate(dx, dy)
                    item.setRect(r)
                elif self.drag_region == 'br':
                    r = QRectF(self._drag_start_rect.topLeft(), pt).normalized()
                    if item._fixed_square:
                        s = max(r.width(), r.height()); r.setWidth(s); r.setHeight(s)
                    item.setRect(r)
                elif self.drag_region == 'tl':
                    r = QRectF(pt, self._drag_start_rect.bottomRight()).normalized()
                    if item._fixed_square:
                        s = max(r.width(), r.height()); r.setWidth(s); r.setHeight(s)
                    item.setRect(r)
                elif self.drag_region == 'tr':
                    r = QRectF(QPointF(self._drag_start_rect.bottomLeft().x(), pt.y()),
                               QPointF(pt.x(), self._drag_start_rect.bottomLeft().y())).normalized()
                    if item._fixed_square:
                        s = max(r.width(), r.height()); r.setWidth(s); r.setHeight(s)
                    item.setRect(r)
                elif self.drag_region == 'bl':
                    r = QRectF(QPointF(pt.x(), self._drag_start_rect.topRight().y()),
                               QPointF(self._drag_start_rect.topRight().x(), pt.y())).normalized()
                    if item._fixed_square:
                        s = max(r.width(), r.height()); r.setWidth(s); r.setHeight(s)
                    item.setRect(r)
            return

        if self.drawing:
            dx = pt.x() - self.start_pt.x(); dy = pt.y() - self.start_pt.y()

            if self.mode == CropMode.CIRCLE:
                item.setRadius((dx**2 + dy**2) ** 0.5)
            elif self.mode == CropMode.SQUARE:
                if mods & Qt.ControlModifier:
                    s = max(abs(dx), abs(dy)) * 2
                    s = max(MIN_SIZE, s); cx, cy = self.start_pt.x(), self.start_pt.y()
                    item.setRect(cx - s/2, cy - s/2, s, s)
                else:
                    s = item.rect().width()
                    item.setRect(pt.x() - s/2, pt.y() - s/2, s, s)
            elif self.mode == CropMode.RECT:
                item.setRect(QRectF(self.start_pt, pt).normalized())

    def on_view_release(self):
        if self.drawing or self.dragging:
            self.drawing = False; self.dragging = False; self.drag_region = None
            self._mark_dirty()
            n = len(self.crop_items)
            self.status_msg(f"选区就绪（共{n}个）→ Enter 批量保存 / Esc 清除当前 / 拖拽可调整")

    def _cancel(self):
        if self.active_item:
            self._remove_active()
            self.status_msg("已清除当前选区")
        elif self.crop_items:
            self._clear_all()
            self.status_msg("已清除全部选区")

    # ── AI DETECT ──
    def _ai_detect(self):
        if not self.src_path:
            QMessageBox.information(self, "提示", "请先打开一张图片。")
            return
        if not self.src_img:
            return

        # Progress
        progress = QProgressDialog("AI 正在识别角色位置...", "取消", 0, 0, self)
        progress.setWindowTitle("AI 识人")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(500)
        progress.show()
        QApplication.processEvents()

        try:
            # Save current image to temp for API
            tmp_img = os.path.join(os.environ.get("TEMP", "/tmp"), "__ck_ai_input.png")
            self.src_img.convert("RGB").save(tmp_img, "PNG")

            result = call_ai_vision(tmp_img, AI_PROMPT)
            progress.close()

            coords = parse_face_coords(result, self.src_img.width, self.src_img.height)
            if not coords:
                QMessageBox.warning(self, "AI 识人", f"未能识别到角色。\n\nAI 返回：\n{result[:300]}")
                return

            # Clear existing and place new crop items
            self._clear_all()

            # Scale factor: compact=1.0, full-head=1.5x the AI-reported head size
            scale = 1.5 if "全头" in self.head_mode_combo.currentText() else 1.0

            iw, ih = self.src_img.width, self.src_img.height
            for parts in coords:
                if len(parts) == 4:
                    label, px, py, head_px = parts
                else:
                    label, px, py = parts[0], parts[1], parts[2]
                    head_px = int(min(iw, ih) * 0.12)

                hs = int(head_px * scale)

                # Clamp to image boundaries
                half = hs // 2
                if self.mode == CropMode.CIRCLE:
                    clamped_cx = max(half, min(px, iw - half))
                    clamped_cy = max(half, min(py, ih - half))
                    item = CropCircle(clamped_cx, clamped_cy, half, label=label)
                elif self.mode == CropMode.SQUARE:
                    cx = max(half, min(px, iw - half))
                    cy = max(half, min(py, ih - half))
                    item = CropRect(QRectF(cx - half, cy - half, hs, hs), fixed_square=True, label=label)
                else:
                    x1 = max(0, px - half)
                    y1 = max(0, py - half)
                    x2 = min(iw, x1 + hs)
                    y2 = min(ih, y1 + hs)
                    if x2 - x1 < MIN_SIZE or y2 - y1 < MIN_SIZE:
                        continue
                    item = CropRect(QRectF(x1, y1, x2 - x1, y2 - y1), fixed_square=False, label=label)
                self.scene.addItem(item)
                self.crop_items.append(item)

            self._select_item(0)
            self._dirty = True
            self._update_crop_btn(); self._update_batch_btn()
            self.status_msg(f"AI 识别到 {len(coords)} 个角色 → 可逐个拖动微调 → Enter 批量保存")
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "AI 错误", str(e))

    # ── CROP ──
    def _crop_one(self, item) -> str:
        """Crop a single item, return output path."""
        if isinstance(item, CropCircle):
            cx, cy = self._to_pixel(item.centerPt)
            r = int(item.radius / self.view._zoom)
            x1, y1 = cx - r, cy - r; x2, y2 = cx + r, cy + r
            mask = Image.new("L", self.src_img.size, 0)
            ImageDraw.Draw(mask).ellipse((x1, y1, x2, y2), fill=255)
            cropped = self.src_img.copy(); cropped.putalpha(mask)
            cropped = cropped.crop((max(0,x1), max(0,y1),
                                     min(self.src_img.width,x2), min(self.src_img.height,y2)))
        else:
            rect = item.rect()
            tl = self.pixmap_item.mapFromScene(rect.topLeft())
            br = self.pixmap_item.mapFromScene(rect.bottomRight())
            x1, y1 = int(tl.x()), int(tl.y()); x2, y2 = int(br.x()), int(br.y())
            x1, x2 = sorted([max(0, x1), min(self.src_img.width, x2)])
            y1, y2 = sorted([max(0, y1), min(self.src_img.height, y2)])
            if x2 - x1 < 5 or y2 - y1 < 5:
                return None
            cropped = self.src_img.crop((x1, y1, x2, y2))

        base = Path(self.src_path).stem
        prefix = self.prefix_edit.text().strip()
        fmt = self.fmt_combo.currentText()
        ext = "jpg" if fmt == "JPEG" else "png"

        # Use item label in filename if available
        if item.label:
            safe_label = re.sub(r'[^\w一-鿿]', '', item.label)[:10]
            if safe_label:
                prefix = f"{prefix}_{safe_label}"

        out = Path(self.src_path).parent / f"{base}{prefix}_{self.counter:02d}.{ext}"

        save_img = cropped
        if fmt == "JPEG" and cropped.mode in ("RGBA", "P"):
            save_img = cropped.convert("RGB")
        save_img.save(str(out), format=fmt)
        return str(out)

    def _do_crop(self):
        """Save only the active selection."""
        item = self.active_item
        if not self.src_img or not item:
            QMessageBox.information(self, "提示", "请先选中一个选区。"); return
        try:
            out = self._crop_one(item)
            if out:
                self.counter += 1; self._refresh_counter()
                self.status_msg(f"已保存：{os.path.basename(out)}")
                self._remove_active()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"裁剪失败：\n{e}")

    def _do_batch_crop(self):
        """Save all selections at once."""
        if not self.crop_items:
            self._do_crop(); return
        try:
            saved = []
            for item in list(self.crop_items):
                out = self._crop_one(item)
                if out:
                    saved.append(os.path.basename(out))
                    self.counter += 1
            self._clear_all()
            self._refresh_counter()
            self.status_msg(f"批量保存完成：{len(saved)} 个文件 → {', '.join(saved[:5])}{'...' if len(saved)>5 else ''}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"批量裁剪失败：\n{e}")

    # ── DRAG & DROP ──
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(('.jpg', '.jpeg', '.png')):
                self.load_image(p); break


STYLE = """
* { font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif; }
QMainWindow { background: #0f0f17; }
QToolBar {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1a1a2e,stop:1 #1e1e32);
    border: none; border-bottom: 1px solid #2a2a40;
    spacing: 6px; padding: 8px 14px;
}
QToolBar QToolButton {
    color: #aab; padding: 8px 16px; border-radius: 8px;
    font-size: 13px; font-weight: 500;
    border: 1px solid transparent;
}
QToolBar QToolButton:hover {
    background: #2a2a42; border: 1px solid #3a3a58; color: #fff;
}
QToolBar QToolButton:checked {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ff69b4,stop:1 #e55a9e);
    color: #fff; font-weight: bold;
    border: 1px solid #ff69b4;
}
QStatusBar {
    background: #0a0a14; color: #6a6a80; font-size: 12px;
    padding: 5px 14px; border-top: 1px solid #1a1a2a;
}
QDockWidget {
    background: #13131f; color: #ccd; border: none;
    border-left: 1px solid #1e1e2e;
}
QGroupBox {
    color: #bbc; font-weight: bold; font-size: 12px;
    border: 1px solid #252535; border-radius: 10px;
    margin-top: 10px; padding-top: 20px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 14px; padding: 0 8px;
    color: #889;
}
QLabel { color: #99a; font-size: 12px; }
QComboBox, QLineEdit, QSpinBox {
    background: #1a1a2c; color: #ccd;
    border: 1px solid #2a2a40; border-radius: 6px;
    padding: 6px 10px; font-size: 13px;
}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover { border: 1px solid #444466; }
QComboBox::drop-down { border: none; padding-right: 8px; }
QComboBox QAbstractItemView {
    background: #1a1a2c; color: #ccd;
    selection-background-color: #ff69b4; border-radius: 4px;
    outline: none;
}
QPushButton {
    background: #222236; color: #99a; border: 1px solid #2a2a40;
    padding: 8px 16px; border-radius: 8px;
    font-weight: 600; font-size: 12px;
}
QPushButton:hover { background: #2a2a48; color: #ccd; border: 1px solid #444466; }
QScrollBar:horizontal, QScrollBar:vertical {
    background: transparent; width: 8px; margin: 2px;
}
QScrollBar::handle {
    background: #2a2a40; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:hover { background: #ff69b4; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; }
QProgressDialog { background: #13131f; color: #ccd; border-radius: 10px; }
QProgressDialog QLabel { color: #ccd; font-size: 13px; }
QSlider::groove:horizontal {
    background: #1a1a2c; height: 6px; border-radius: 3px;
    border: 1px solid #2a2a40;
}
QSlider::handle:horizontal {
    background: #ff69b4; width: 16px; height: 16px;
    margin: -6px 0; border-radius: 8px;
}
QSlider::sub-page:horizontal { background: #ff69b4; border-radius: 3px; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        import ctypes
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.windll.GetForegroundWindow(), 20, ctypes.byref(ctypes.c_int(2)), 4)
    except: pass
    img_path = sys.argv[1] if len(sys.argv) > 1 else None
    win = CropKing(img_path); win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
