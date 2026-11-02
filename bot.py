"""观域（Voyant）——AI 屏幕助手工作台

左侧监视框：把屏幕上任意区域/窗口框起来，画面变化自动唤起 AI 视觉识别；
AI 可使用鼠标/键盘工具操作框内内容，并用 markdown 文件管理长期记忆。
右侧工作台：状态、大脑（提示词/工具可视化编辑）、记忆管理、执行记录。

运行：双击 start.bat
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
DATA_DIR = BASE / "data"
PICKER = BASE / "pick.py"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# DPI 感知：保证坐标是真实物理像素
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    import win32gui
except ImportError:
    win32gui = None

from PIL import Image, ImageChops, ImageDraw, ImageGrab

U32 = ctypes.windll.user32


# ================================================================ 配置
DEFAULT_PROMPT = """你是一个电脑屏幕操作助手。用户把屏幕上的一块区域框成"监视框"给你看，你通过画面理解内容，用工具完成操作任务。每个工具的详细用法（什么时候用、参数怎么填、出错了怎么办、JSON 调用示例）都在工具定义的【使用说明】里，按各工具的说明选用工具。

## 工作原则
1. 先观察画面、想清楚目标再动手；每次屏幕操作后程序会自动附上最新截图，核对效果后再做下一步，不要凭猜测连点。
2. 谨慎对待不可逆操作（发送、删除、支付、下单）。绝不在用户没有明确要求的情况下发送消息给任何人。
3. 完成任务或需要用户决定时，用简洁的中文回复结论，停止调用工具。"""

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-5.3-flash",
    "system_prompt": DEFAULT_PROMPT,
    "memory_dir": str(BASE / "data" / "记忆"),
    "history_dir": str(BASE / "data"),
    "boxes": [],
    "auto_run": True,
    "dry_run": False,
    "check_interval": 1.0,
    "change_threshold": 6,
    "trigger_cooldown": 20,
    "max_steps": 12,
    "context_budget": 800000,     # 每个会话的上下文预算（token，粗估）
    "compress_keep_recent": 8,    # 压缩时保留最近几条原文
    "thinking": "disabled",       # 默认值（实际以 config.json 的用户设置为准）
    "active_session": "默认会话",  # 聊天与画面监控共用的当前会话
    "web_port": 8770,
    "open_browser": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[配置] 读取失败，使用默认: {e}")
    return cfg


def save_config() -> None:
    CONFIG_FILE.write_text(
        json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()

# ================================================================ 运行状态
EVENTS: deque = deque(maxlen=80)
STEPS: deque = deque(maxlen=300)
STATS = {"runs": 0, "tool_calls": 0, "mem_writes": 0, "errors": 0}
STATE = {"busy": False, "stop": False, "paused": False, "thinking": ""}
BOXES: dict[int, dict] = {}
_next_box_id = [1]
_lock = threading.Lock()
_agent_lock = threading.Lock()


def log_event(text: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    with _lock:
        EVENTS.append(f"[{stamp}] {text}")
    print(f"[事件] {text}", flush=True)


def add_step(kind: str, text: str) -> None:
    with _lock:
        STEPS.append({"t": time.strftime("%H:%M:%S"), "kind": kind,
                      "text": text[:600]})


# ================================================================ GLM
class GLM:
    def __init__(self):
        self.client = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            if self.client is None:
                from openai import OpenAI
                self.client = OpenAI(api_key=CONFIG["api_key"],
                                     base_url=CONFIG["base_url"], timeout=180)
            return self.client

    def abort(self):
        """真实中断在途请求：关闭底层连接，正在等待的调用会立刻报错返回。"""
        with self._lock:
            c = self.client
            self.client = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        if STATE["stop"] or STATE["paused"]:
            raise RuntimeError("已被用户停止")
        kwargs = {"model": CONFIG["model"], "messages": messages,
                  "temperature": 0.5, "stream": True}
        if tools:
            kwargs["tools"] = tools
        th = str(CONFIG.get("thinking", "auto"))
        if th == "auto":
            # 带画面（视觉定位）自动开思考保证精度；纯文字自动关保证速度
            has_image = any(
                isinstance(m.get("content"), list)
                and any(isinstance(p, dict) and p.get("type") == "image_url"
                        for p in m["content"])
                for m in messages)
            th = "enabled" if has_image else "disabled"
        if th in ("enabled", "disabled"):
            kwargs["extra_body"] = {"thinking": {"type": th}}
        resp = self._ensure().chat.completions.create(**kwargs)
        # 流式接收：思考文本实时写入 STATE["thinking"]，前端轮询展示
        parts: list[str] = []
        calls: dict = {}
        STATE["thinking"] = ""
        for chunk in resp:
            if STATE["stop"] or STATE["paused"]:
                raise RuntimeError("已被用户停止")
            for choice in (chunk.choices or []):
                d = choice.delta
                extra = getattr(d, "model_extra", None) or {}
                rc = getattr(d, "reasoning_content", None) \
                    or extra.get("reasoning_content")
                if rc:
                    STATE["thinking"] = (STATE["thinking"] + rc)[-12000:]
                if d.content:
                    parts.append(d.content)
                for tc in (d.tool_calls or []):
                    idx = getattr(tc, "index", None)
                    slot = calls.setdefault(
                        idx if idx is not None else len(calls),
                        {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if fn.name:
                            slot["name"] = fn.name
                        if fn.arguments:
                            slot["args"] += fn.arguments
        return {"content": "".join(parts),
                "tool_calls": [{"id": s["id"], "name": s["name"],
                                "args": s["args"] or "{}"}
                               for _, s in sorted(calls.items(),
                                                  key=lambda kv: str(kv[0]))]}


GLM_CLIENT = GLM()

# ================================================================ 记忆（markdown 文件）
def mem_root() -> Path:
    p = Path(CONFIG["memory_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def mem_safe(rel: str) -> Path:
    rel = (rel or "").strip().strip("/\\")
    if not rel or ".." in rel:
        raise ValueError("路径不合法")
    p = (mem_root() / rel).resolve()
    if not str(p).startswith(str(mem_root().resolve())):
        raise ValueError("路径越界")
    return p


def mem_save(folder: str, filename: str, content: str) -> str:
    folder = re.sub(r'[\\/:*?"<>|]', "_", (folder or "").strip()) or "未分类"
    filename = re.sub(r'[\\/:*?"<>|]', "_", (filename or "").strip())
    if not filename:
        raise ValueError("文件名不能为空")
    if not filename.endswith(".md"):
        filename += ".md"
    p = mem_root() / folder / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    STATS["mem_writes"] += 1
    return str(p.relative_to(mem_root()))


def mem_read(rel: str) -> str:
    return mem_safe(rel).read_text(encoding="utf-8")[:6000]


def mem_delete(rel: str) -> bool:
    p = mem_safe(rel)
    if p.is_file():
        p.unlink()
        try:
            p.parent.rmdir()
        except OSError:
            pass
        return True
    return False


def mem_tree() -> dict:
    root = mem_root()
    folders = []
    for d in sorted(root.iterdir()):
        if d.is_dir():
            files = [{"name": f.name, "size": f.stat().st_size}
                     for f in sorted(d.iterdir()) if f.is_file()]
            folders.append({"name": d.name, "files": files})
    loose = [{"name": f.name, "size": f.stat().st_size}
             for f in sorted(root.iterdir()) if f.is_file()]
    return {"folders": folders, "loose": loose}


def mem_search(kw: str) -> list[str]:
    hits = []
    for p in mem_root().rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = str(p.relative_to(mem_root()))
        if kw in rel:
            hits.append(f"{rel}（文件名匹配）")
        elif kw in text:
            i = text.find(kw)
            snippet = text[max(0, i - 40): i + 80].replace("\n", " ")
            hits.append(f"{rel}：…{snippet}…")
        if len(hits) >= 20:
            break
    return hits


# ================================================================ 鼠标键盘
MOUSE_LEFT_DOWN, MOUSE_LEFT_UP = 0x02, 0x04
MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP = 0x08, 0x10
MOUSE_WHEEL = 0x0800

VK: dict[str, int] = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK[_c] = 0x41 + _i
for _i in range(10):
    VK[str(_i)] = 0x30 + _i
VK.update({"enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
           "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
           "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
           "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
           "right": 0x27, "printscreen": 0x2C})
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i
MOD = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}


def _kb(vk: int, up: bool = False) -> None:
    U32.keybd_event(vk, 0, 2 if up else 0, 0)


def press_combo(keys: str) -> str:
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    if not parts:
        raise ValueError("按键为空")
    vks = []
    for p in parts:
        if p in MOD:
            vks.append(MOD[p])
        elif p in VK:
            vks.append(VK[p])
        else:
            raise ValueError(f"不认识的按键: {p}")
    mods, key = vks[:-1], vks[-1]
    for m in mods:
        _kb(m)
        time.sleep(0.02)
    _kb(key)
    time.sleep(0.04)          # 按下持续一小段：应用才稳定收到，防吞键
    _kb(key, up=True)
    for m in reversed(mods):
        _kb(m, up=True)
    return keys


def type_text(text: str) -> None:
    import pyperclip
    pyperclip.copy(text)
    time.sleep(0.05)
    press_combo("ctrl+v")


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _mouse_move_to(sx: int, sy: int) -> None:
    """平滑移动鼠标到目标点：部分应用需要经历 hover 过程才响应点击。"""
    pt = _POINT()
    U32.GetCursorPos(ctypes.byref(pt))
    x0, y0 = pt.x, pt.y
    steps = 6
    for i in range(1, steps + 1):
        U32.SetCursorPos(int(x0 + (sx - x0) * i / steps),
                         int(y0 + (sy - y0) * i / steps))
        time.sleep(0.02)


def _hit_window_title(sx: int, sy: int) -> str:
    """查询屏幕某一点最上层的顶层窗口标题：点击回执的命中校验/遮挡感知。"""
    try:
        U32.WindowFromPoint.restype = ctypes.c_void_p
        U32.WindowFromPoint.argtypes = [_POINT]
        hwnd = U32.WindowFromPoint(_POINT(int(sx), int(sy)))
        if win32gui is None or not hwnd:
            return "?"
        root = win32gui.GetAncestor(int(hwnd), 2)   # GA_ROOT
        return (win32gui.GetWindowText(root) or "?")[:30]
    except Exception:
        return "?"


# ================================================================ 本地 OCR（Windows 自带引擎，离线毫秒级）
_ocr_lock = threading.Lock()
_OCR_DEPS_HINT = ("本地 OCR 缺少依赖，请执行: pip install winrt-runtime "
                  "winrt-Windows.Media.Ocr winrt-Windows.Globalization "
                  "winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams")


def _ocr_lines(image_bytes: bytes) -> list[tuple[str, float, float, float, float]]:
    """Windows 自带 OCR：返回 [(文字, x, y, w, h), ...]，坐标为图像像素。"""
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    from winrt.windows.graphics.imaging import BitmapDecoder

    async def _run():
        engine = OcrEngine.try_create_from_language(Language("zh-CN"))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("系统缺少中文 OCR 语言包（设置→时间和语言→语言 中添加中文）")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(image_bytes)
        await writer.store_async()
        await writer.flush_async()
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        out = []
        for line in result.lines:
            try:
                xs = [w.bounding_rect.x for w in line.words]
                ys = [w.bounding_rect.y for w in line.words]
                xe = [w.bounding_rect.x + w.bounding_rect.width for w in line.words]
                ye = [w.bounding_rect.y + w.bounding_rect.height for w in line.words]
                out.append((line.text, min(xs), min(ys),
                            max(xe) - min(xs), max(ye) - min(ys)))
            except Exception:
                continue
        return out

    with _ocr_lock:
        return asyncio.run(_run())


def _ocr_box_anchors(box: dict) -> list[dict]:
    """全分辨率、无网格、无损截取监视框并 OCR。

    返回锚点列表：文字 + 屏幕中心坐标 + 框内 0~1000 坐标。
    注意：必须用干净的全分辨率原图，网格叠加图会干扰识别。
    截图瞬间若遇窗口拖动/动画会识别成乱码，故带质量重试（取汉字最多的那次）。
    """
    rect = resolve_rect(box)
    if not rect:
        raise ValueError(f"框「{box['name']}」当前不可见（已最小化或已关闭）")
    box["rect"] = rect
    l, t, w, h = rect

    def cjk_count(anchors):
        return sum(sum(1 for ch in a["text"] if ord(ch) > 0x2E7F)
                   for a in anchors)

    best, best_n = [], -1
    for attempt in range(3):
        try:
            img = ImageGrab.grab(bbox=(l, t, l + w, t + h),
                                 all_screens=True).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            anchors = []
            for text, x, y, ww, hh in _ocr_lines(buf.getvalue()):
                anchors.append({
                    "text": text,
                    "sx": int(l + x + ww / 2), "sy": int(t + y + hh / 2),
                    "fx": round((x + ww / 2) / max(1, w) * 1000),
                    "fy": round((y + hh / 2) / max(1, h) * 1000),
                })
        except Exception as e:
            log_event(f"OCR 第{attempt + 1}次失败: {e}")
            time.sleep(0.4)
            continue
        n = cjk_count(anchors)
        if n > best_n:
            best, best_n = anchors, n
        if n >= 20:   # 质量足够，提前结束
            break
        time.sleep(0.4)
    return best if best_n >= 2 else []


def _norm_text(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def h_click_text(args):
    """按文字精确点击：OCR 实时定位，点名即中，天然免疫画面时间差与遮挡。"""
    text = _norm_text(args.get("text"))
    if not text:
        raise ValueError("text 为空")
    box = _get_box(args)
    try:
        nth = max(1, int(args.get("nth", 1) or 1))
    except (TypeError, ValueError):
        nth = 1
    anchors = _ocr_box_anchors(box)
    hits = [a for a in anchors if text in _norm_text(a["text"])]
    if not hits:
        sample = "、".join(_norm_text(a["text"])[:10] for a in anchors[:15])
        return (f"⚠️ 没找到文字「{text}」。当前画面可见的文字有：{sample}…"
                "（目标可能被遮挡、不在框内，或写法不一致）")
    if nth > len(hits):
        return f"只找到 {len(hits)} 处「{text}」，没有第 {nth} 处"
    a = hits[nth - 1]
    sx, sy = a["sx"], a["sy"]
    if CONFIG["dry_run"]:
        return (f"[演练] 点击文字「{text}」→「{a['text'][:30]}」"
                f"目标屏幕({sx},{sy})（未真正执行）")
    _mouse_move_to(sx, sy)
    time.sleep(0.08)
    U32.mouse_event(MOUSE_LEFT_DOWN, 0, 0, 0, 0)
    U32.mouse_event(MOUSE_LEFT_UP, 0, 0, 0, 0)
    hit = _hit_window_title(sx, sy)
    return f"[执行] 点击文字「{text}」 屏幕({sx},{sy}) 命中窗口=「{hit}」"


def h_read_text(args):
    """本地 OCR 整屏转录：确定性读出全部文字及坐标（快、准、免 API）。"""
    box = _get_box(args)
    anchors = _ocr_box_anchors(box)
    if not anchors:
        return "OCR 没有识别到任何文字（画面可能被遮挡或为纯图像）"
    lines = [f"({a['fx']},{a['fy']}) {a['text'][:60]}" for a in anchors]
    return ("画面文字（括号内为0~1000坐标，可直接用于点击）：\n"
            + "\n".join(lines[:60]))


# ================================================================ 监视框
def resolve_rect(box: dict) -> list[int] | None:
    if box["mode"] == "region":
        r = box["rect"]
        return [int(r[0]), int(r[1]), int(r[2]), int(r[3])]
    if win32gui is None:
        return None
    h = int(box.get("hwnd") or 0)

    def _valid(hh):
        try:
            return (win32gui.IsWindow(hh) and not win32gui.IsIconic(hh)
                    and win32gui.IsWindowVisible(hh))
        except Exception:
            return False

    if not _valid(h) and box.get("title"):
        # 微信重启/窗口句柄失效：按标题自动重新绑定
        def rebind(candidate, _):
            nonlocal h
            if not _valid(h) and win32gui.GetWindowText(candidate) == box.get("title"):
                h = candidate
                box["hwnd"] = candidate
        try:
            win32gui.EnumWindows(rebind, None)
        except Exception:
            pass
        if _valid(h):
            log_event(f"框「{box['name']}」已自动重新绑定窗口（句柄更新为 {h}）")
    try:
        if not _valid(h):
            return None
        l, t = win32gui.ClientToScreen(h, (0, 0))
        w, hh = win32gui.GetClientRect(h)[2], win32gui.GetClientRect(h)[3]
        if w < 60 or hh < 60:
            return None
        return [l, t, w, hh]
    except Exception:
        return None


def capture_box(box: dict) -> float | None:
    """截图并计算与上一次的变化度(0~255 均值差)。返回变化度；失败返回 None。"""
    rect = resolve_rect(box)
    if not rect:
        box["error"] = "窗口已关闭/最小化或区域无效"
        return None
    box["error"] = ""
    box["rect"] = rect
    l, t, w, h = rect
    try:
        img = ImageGrab.grab(bbox=(l, t, l + w, t + h), all_screens=True).convert("RGB")
    except Exception as e:
        box["error"] = f"截图失败: {e}"
        return None

    # 原图直出（不做缩放变换）；仅在超大窗口时保底压到 2200，防 API 超限
    ratio = min(1.0, 2200 / img.width, 2200 / img.height)
    if ratio < 1.0:
        img_full = img.resize((int(img.width * ratio), int(img.height * ratio)))
    else:
        img_full = img

    # 叠加 10×10 定位格：绿色细线，不标任何数字，行列由模型按工具说明自己数。
    # 主格线绿色半透明（绿与常见 UI 颜色区分度高、也不干扰深浅底），格内不拉
    # 细分通线（会从小图标身上穿过），只在主格线上画短刻度（每 25 单位一个）。
    try:
        gw, gh = img_full.size
        ov = Image.new("RGBA", img_full.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for i in range(1, 10):            # 主格：每 100 单位一条绿线
            x, y = gw * i // 10, gh * i // 10
            od.line([(x, 0), (x, gh)], fill=(0, 170, 0, 120), width=1)
            od.line([(0, y), (gw, y)], fill=(0, 170, 0, 120), width=1)
        for i in range(1, 10):            # 刻度：主格线上每 25 单位一个短刻度
            x, y = gw * i // 10, gh * i // 10
            for k in range(1, 40):
                if k % 4 == 0:
                    continue
                ty, tx = gh * k // 40, gw * k // 40
                od.line([(x - 5, ty), (x + 4, ty)], fill=(0, 170, 0, 160), width=1)
                od.line([(tx, y - 5), (tx, y + 4)], fill=(0, 170, 0, 160), width=1)
        img_full = Image.alpha_composite(
            img_full.convert("RGBA"), ov).convert("RGB")
    except Exception:
        pass

    buf = io.BytesIO()
    img_full.save(buf, "PNG")   # 无损：文字边缘锐利，UI 截图体积也不大
    if len(buf.getvalue()) > 2_500_000:   # 个别照片型画面 PNG 过大时保底用高质量 JPEG
        buf = io.BytesIO()
        img_full.save(buf, "JPEG", quality=85)
    with _lock:
        box["shot"] = buf.getvalue()
        box["shot_mime"] = "image/png" if buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n" \
            else "image/jpeg"
        box["frame"] = int(box.get("frame") or 0) + 1   # 帧号：前端用它判断有无新画面
        box["served_size"] = (w, h)   # 该帧对应的窗口尺寸：点击时做左上锚定映射

    small = img.convert("L").resize((160, max(1, int(160 * img.height / img.width))))
    prev = box.get("prev_small")
    box["prev_small"] = small
    if prev is None or prev.size != small.size:
        return 0.0   # 首帧或尺寸变化：只更新基线
    hist = ImageChops.difference(small, prev).histogram()
    total = sum(hist)
    return sum(i * c for i, c in enumerate(hist)) / max(1, total)


def box_by_id(bid) -> dict | None:
    try:
        return BOXES.get(int(bid))
    except (TypeError, ValueError):
        return None


def enabled_boxes() -> list[dict]:
    return [b for b in BOXES.values() if b.get("monitor")]


def load_boxes() -> None:
    for b in CONFIG.get("boxes", []):
        b = dict(b)
        b.setdefault("monitor", True)
        BOXES[b["id"]] = b
        _next_box_id[0] = max(_next_box_id[0], b["id"] + 1)


def save_boxes() -> None:
    out = []
    for b in BOXES.values():
        out.append({k: v for k, v in b.items()
                    if k in ("id", "name", "mode", "hwnd", "title", "rect", "monitor")})
    CONFIG["boxes"] = out
    save_config()


# ================================================================ 工具
def _to_screen(box: dict, x, y) -> tuple[int, int, int, int]:
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        raise ValueError("坐标必须是数字")
    if not (0 <= fx <= 1000 and 0 <= fy <= 1000):
        raise ValueError("坐标必须是 0~1000 的框内相对值")
    l, t, w, h = box["rect"]
    # 关键：AI 的坐标基于它所见的那帧（截图时的窗口尺寸）。
    # 窗口缩放后，固定像素位置的元素（工具栏图标、列表行）用"左上锚定"
    # 映射回屏幕更准（像素位置基本不变），按比例缩放反而会双向漂移。
    # 越界钳制回当前框内。
    sw, sh = box.get("served_size") or (w, h)
    sx = int(l + fx / 1000 * sw)
    sy = int(t + fy / 1000 * sh)
    sx = max(l, min(l + w - 1, sx))
    sy = max(t, min(t + h - 1, sy))
    return sx, sy, w, h


def _cell_to_fx(args: dict, default_row=5, default_col=5):
    """从 行/列/格内偏移 参数换算 0~1000 坐标；越界直接报错打回。"""
    try:
        row = int(args.get("row", default_row))
        col = int(args.get("col", default_col))
        ox = float(args.get("ox", 50))
        oy = float(args.get("oy", 50))
    except (TypeError, ValueError):
        raise ValueError("row/col/ox/oy 必须是数字")
    if not (1 <= row <= 10 and 1 <= col <= 10):
        raise ValueError(f"row/col 必须在 1~10（收到 row={row}, col={col}）——"
                         "请按画面上每格左上角的'行-列'标注选择格子")
    if not (0 <= ox <= 100 and 0 <= oy <= 100):
        raise ValueError(f"格内偏移 ox/oy 必须在 0~100（收到 ox={ox}, oy={oy}）")
    return ((col - 1) * 100 + ox, (row - 1) * 100 + oy, row, col, ox, oy)


def _get_box(args) -> dict:
    b = box_by_id(args.get("box_id"))
    if b is None:
        ens = enabled_boxes() or list(BOXES.values())
        if len(ens) == 1:
            b = ens[0]
        elif not BOXES:
            raise ValueError("还没有创建监视框")
        else:
            raise ValueError("有多个监视框，请用 box_id 指明（见画面标注）")
    # 关键：执行前实时重新解析窗口位置/大小。
    # AI 看到的是之前的截图，若窗口事后被移动/调整过，只有用最新 rect
    # 换算，0~1000 相对坐标才能落在正确位置
    fresh = resolve_rect(b)
    if fresh:
        b["rect"] = fresh
    if not b.get("rect"):
        raise ValueError(f"框「{b['name']}」还没有可用画面（可能窗口不可见）")
    return b


def _act_click(args, kind: str) -> str:
    box = _get_box(args)
    has_x, has_y = args.get("ox") is not None, args.get("oy") is not None
    if not has_x and not has_y:
        return _aim_cell(box, args)   # 只填 row/col＝先瞄准该格，不点击
    if has_x != has_y:
        return "ox/oy 要么都填（直接点击），要么都不填（先瞄准放大）；只填一个无法定位"
    fx, fy, row, col, ox, oy = _cell_to_fx(args)
    sx, sy, _, _ = _to_screen(box, fx, fy)
    refined = LAST_AIM.get("cell") == (box["id"], row, col)
    LAST_AIM["cell"] = None
    if not refined:   # 瞄准后的点击不再附落点放大图：模型刚看过该格的放大图
        LAST_ZOOM["jpeg"] = _zoom_capture(box, sx, sy, fx, fy)
    zoom_tag = "" if refined else "落点放大图已附"
    if CONFIG["dry_run"]:
        return (f"[演练] {kind}点击 格({row},{col})内({ox:.0f},{oy:.0f}) "
                f"目标屏幕({sx},{sy})（未真正执行）{zoom_tag}")
    _mouse_move_to(sx, sy)          # 平滑移动：部分应用需要 hover 才响应
    time.sleep(0.08)
    if kind == "right":
        U32.mouse_event(MOUSE_RIGHT_DOWN, 0, 0, 0, 0)
        U32.mouse_event(MOUSE_RIGHT_UP, 0, 0, 0, 0)
    else:
        for _ in range(2 if kind == "double" else 1):
            U32.mouse_event(MOUSE_LEFT_DOWN, 0, 0, 0, 0)
            U32.mouse_event(MOUSE_LEFT_UP, 0, 0, 0, 0)
            time.sleep(0.05)
    hit = _hit_window_title(sx, sy)
    return (f"[执行] {kind}点击 格({row},{col})内({ox:.0f},{oy:.0f}) "
            f"屏幕({sx},{sy}) 命中窗口=「{hit}」{zoom_tag}")


def h_click(args):
    return _act_click(args, "left")


def h_double(args):
    return _act_click(args, "double")


def h_rclick(args):
    return _act_click(args, "right")


def h_drag(args):
    box = _get_box(args)
    fx1, fy1, r1, c1, ox1, oy1 = _cell_to_fx(
        {"row": args.get("row1"), "col": args.get("col1"),
         "ox": args.get("ox1", 50), "oy": args.get("oy1", 50)}, 5, 5)
    fx2, fy2, r2, c2, ox2, oy2 = _cell_to_fx(
        {"row": args.get("row2"), "col": args.get("col2"),
         "ox": args.get("ox2", 50), "oy": args.get("oy2", 50)}, 5, 5)
    x1, y1, _, _ = _to_screen(box, fx1, fy1)
    x2, y2, _, _ = _to_screen(box, fx2, fy2)
    LAST_ZOOM["jpeg"] = _zoom_capture(box, x1, y1, fx1, fy1)   # 起点放大核对，随下一轮注入
    if CONFIG["dry_run"]:
        return (f"[演练] 拖拽 格({r1},{c1})内({ox1:.0f},{oy1:.0f}) → "
                f"格({r2},{c2})内({ox2:.0f},{oy2:.0f})（未真正执行）落点放大图已附")
    _mouse_move_to(x1, y1)
    time.sleep(0.08)
    U32.mouse_event(MOUSE_LEFT_DOWN, 0, 0, 0, 0)
    for i in range(1, 9):
        U32.SetCursorPos(int(x1 + (x2 - x1) * i / 8), int(y1 + (y2 - y1) * i / 8))
        time.sleep(0.02)
    U32.mouse_event(MOUSE_LEFT_UP, 0, 0, 0, 0)
    hit = _hit_window_title(x2, y2)
    return (f"[执行] 拖拽 格({r1},{c1})→格({r2},{c2}) "
            f"屏幕({x1},{y1})→({x2},{y2}) 命中窗口=「{hit}」落点放大图已附")


def _small_now(box) -> object:
    """当前框画面的缩小灰度图（用于前后对比）。"""
    rect = resolve_rect(box)
    if not rect:
        return None
    l, t, w, h = rect
    img = ImageGrab.grab(bbox=(l, t, l + w, t + h), all_screens=True).convert("L")
    return img.resize((120, max(1, int(120 * img.height / img.width))))


def _diff_score(a, b) -> float:
    if a is None or b is None or a.size != b.size:
        return 99.0
    hist = ImageChops.difference(a, b).histogram()
    total = sum(hist)
    return sum(i * c for i, c in enumerate(hist)) / max(1, total)


def h_scroll(args):
    box = _get_box(args)
    fx, fy, row, col, ox, oy = _cell_to_fx(
        {"row": args.get("row", 5), "col": args.get("col", 5),
         "ox": args.get("ox", 50), "oy": args.get("oy", 50)})
    sx, sy, _, _ = _to_screen(box, fx, fy)
    direction = str(args.get("direction", "down")).lower()
    amount = max(1, min(10, int(args.get("amount", 3))))
    delta = -120 * amount if direction == "up" else 120 * amount
    if CONFIG["dry_run"]:
        return (f"[演练] 滚动 {direction} {amount} 格 目标屏幕({sx},{sy})"
                "（每格约100像素，未真正执行）")
    U32.SetCursorPos(sx, sy)
    time.sleep(0.05)
    U32.mouse_event(MOUSE_WHEEL, 0, 0, delta, 0)
    time.sleep(0.8)   # 等滚动动画结束再判断
    pre = _small_now(box)
    post = _small_now(box)
    moved = _diff_score(pre, post)
    tries = 1
    while moved < 1.5 and tries < 3:   # 画面没动：可能被吞了/惯性未结束，补滚
        time.sleep(0.3)
        U32.mouse_event(MOUSE_WHEEL, 0, 0, delta, 0)
        time.sleep(0.9)
        post = _small_now(box)
        moved = _diff_score(pre, post)
        tries += 1
    hit = _hit_window_title(sx, sy)
    tag = "向下" if direction == "down" else "向上"
    if moved < 1.5:
        return (f"[执行] {tag}滚动{amount}格×{tries}次 画面均无变化"
                f" 屏幕({sx},{sy}) 命中窗口=「{hit}」——可能已到顶/到底")
    return (f"[执行] {tag}滚动{amount}格×{tries}次生效 屏幕({sx},{sy}) "
            f"命中窗口=「{hit}」")


def h_type(args):
    text = str(args.get("text", ""))
    if not text:
        raise ValueError("text 为空")
    text = text[:2000]
    # 焦点安全护栏：焦点窗口不在监视框内时拒绝输入，防止打错窗口
    box = None
    try:
        box = _get_box({})
    except Exception:
        pass
    fi = _focus_info()
    if not _rects_overlap(fi.get("rect"), box.get("rect") if box else None) \
            and not args.get("force"):
        return ("⚠️ 已阻止输入：当前焦点窗口是「" + fi.get("title", "?")
                + "」，不在监视框内——文字可能打错地方。"
                "请先用点击工具点中目标输入框；确认就要输在这里的话，"
                "带 force=true 重试。")
    if not CONFIG["dry_run"]:
        type_text(text)
    tag = "[演练模式] " if CONFIG["dry_run"] else ""
    return (f"{tag}已向焦点「{fi.get('title', '?')[:24]}」输入 {len(text)} 字"
            "（剪贴板粘贴）")


def h_keys(args):
    keys = str(args.get("keys", ""))
    try:
        repeat = max(1, min(3, int(args.get("repeat", 1))))
    except (TypeError, ValueError):
        repeat = 1
    if CONFIG["dry_run"]:
        return f"[演练模式] 未真正按键: {keys}" + (f" ×{repeat}" if repeat > 1 else "")
    for i in range(repeat):
        press_combo(keys)
        if i < repeat - 1:
            time.sleep(0.15)
    return f"已按下 {keys}" + (f" ×{repeat}" if repeat > 1 else "")


def _focus_info() -> dict:
    """当前前台窗口信息（标题 + 屏幕矩形），用于输入前的安全校验。"""
    if win32gui is None:
        return {"title": "?", "rect": None}
    try:
        h = win32gui.GetForegroundWindow()
        title = (win32gui.GetWindowText(h) or "")[:40]
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            rect = [l, t, r - l, b - t]
        except Exception:
            rect = None
        return {"title": title or "?", "rect": rect}
    except Exception:
        return {"title": "?", "rect": None}


def _rects_overlap(a, b) -> bool:
    if not a or not b:
        return False
    al, at, aw, ah = a
    bl, bt, bw, bh = b
    return not (al + aw <= bl or bl + bw <= al
                or at + ah <= bt or bt + bh <= at)


def h_focus(args):
    fi = _focus_info()
    try:
        box = _get_box({})
    except Exception as e:
        return f"前台窗口: 「{fi.get('title', '?')}」位置{fi.get('rect')}（{e}）"
    inside = _rects_overlap(fi.get("rect"), box.get("rect"))
    tip = "是，可以安全输入" if inside else \
        "否——文字会打进这里！先点击框内目标输入框"
    return (f"前台窗口: 「{fi.get('title', '?')}」 位置: {fi.get('rect')}；"
            f"焦点在监视框「{box['name']}」内: {tip}")


def h_clear(args):
    if CONFIG["dry_run"]:
        return "[演练模式] 将执行 ctrl+a + delete 清空当前焦点输入框"
    press_combo("ctrl+a")
    time.sleep(0.08)
    press_combo("delete")
    return "已清空当前焦点输入框（ctrl+a + delete）"


def h_read_screen(args):
    """整屏转录监视框文字：一次模型调用读全部内容，替代连环滚动。"""
    box = _get_box(args)
    capture_box(box)
    jpeg = box.get("shot")
    if not jpeg:
        raise ValueError("无法获取监视框画面")
    b64 = base64.b64encode(jpeg).decode()
    q = (str(args.get("question") or "").strip()
         or "逐字转录画面中所有可读文字，保持原有顺序与分组；没有文字就简要描述画面内容")
    r = GLM_CLIENT.chat([{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}])
    return "画面文字转录：\n" + str(r.get("content") or "(无内容)")[:4000]


def h_wait(args):
    try:
        sec = min(5.0, max(0.3, float(args.get("seconds", 1))))
    except (TypeError, ValueError):
        sec = 1.0
    time.sleep(sec)
    return f"已等待 {sec}s"


LAST_ZOOM = {"jpeg": None}   # 点击类工具自带的落点放大图，随下一轮消息注入给模型
LAST_AIM = {"jpeg": None, "cell": None}   # 瞄准模式（只填 row/col）生成的格子放大图；
                                          # cell=已瞄准的格，其后的点击不再重复附落点图


def _zoom_capture(box: dict, sx: int, sy: int, fx, fy, size: int = 300):
    """截取落点附近画面并放大：红十字标出落点，图上标注区域与落点的0~1000坐标。

    点击/拖拽工具的内置步骤（不是独立工具）：执行动作前抓拍，保证拍到的
    是点击瞬间的画面。返回 JPEG 字节；截图失败返回 None（不影响动作本身）。
    """
    try:
        l, t, bw, bh = box["rect"]
        rx1 = max(l, sx - size // 2)
        ry1 = max(t, sy - size // 2)
        rx2 = min(l + bw, rx1 + size)
        ry2 = min(t + bh, ry1 + size)
        rx1 = max(l, rx2 - size)
        ry1 = max(t, ry2 - size)
        img = ImageGrab.grab(bbox=(rx1, ry1, rx2, ry2), all_screens=True).convert("RGB")
        scale = max(1, min(4, 700 // max(1, img.width)))
        if scale > 1:
            img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        d = ImageDraw.Draw(img)
        cx, cy = (sx - rx1) * scale, (sy - ry1) * scale
        d.line([(cx - 34, cy), (cx + 34, cy)], fill=(255, 40, 40), width=2)
        d.line([(cx, cy - 34), (cx, cy + 34)], fill=(255, 40, 40), width=2)
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(255, 40, 40), width=2)
        try:   # 直接标注对应坐标，省去模型心算换算
            from PIL import ImageFont
            fnt = ImageFont.load_default()
            fx1 = round((rx1 - l) / max(1, bw) * 1000)
            fy1 = round((ry1 - t) / max(1, bh) * 1000)
            fx2 = round((rx2 - l) / max(1, bw) * 1000)
            fy2 = round((ry2 - t) / max(1, bh) * 1000)
            d.text((6, 6), f"左上({fx1},{fy1})", fill=(255, 40, 40), font=fnt)
            d.text((max(6, img.width - 120), img.height - 20),
                   f"右下({fx2},{fy2})", fill=(255, 40, 40), font=fnt)
            d.text((min(img.width - 120, cx + 12), max(2, cy - 24)),
                   f"十字=({fx:.0f},{fy:.0f})", fill=(255, 40, 40), font=fnt)
        except Exception:
            pass
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def _aim_cell(box: dict, args: dict) -> str:
    """点击工具的瞄准模式（只填 row/col、不填 ox/oy）：不点击，回传该格放大图。

    图内横轴刻度=ox、纵轴刻度=oy（0~100，每 20 单位标数字），模型精读后
    把四个参数填全再次调用即完成点击。这是"先瞄后打"，专治小图标第一击偏移。
    """
    try:
        row = int(args.get("row"))
        col = int(args.get("col"))
    except (TypeError, ValueError):
        raise ValueError("row/col 必须是数字")
    if not (1 <= row <= 10 and 1 <= col <= 10):
        raise ValueError(f"row/col 必须在 1~10（收到 row={row}, col={col}）")
    l, t, w, h = box["rect"]
    x0 = int(l + (col - 1) * w / 10)
    x1 = int(l + col * w / 10)
    y0 = int(t + (row - 1) * h / 10)
    y1 = int(t + row * h / 10)
    x1, y1 = min(x1, l + w), min(y1, t + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("该格太小，无法放大瞄准")
    img = ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True).convert("RGB")
    scale = max(2, min(6, max(1, 400 // max(img.width, img.height))))
    img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(1, 10):                # 细线：每 10 单位一条
        x = img.width * i // 10
        y = img.height * i // 10
        od.line([(x, 0), (x, img.height)], fill=(0, 170, 0, 70), width=1)
        od.line([(0, y), (img.width, y)], fill=(0, 170, 0, 70), width=1)
    for v in (20, 40, 60, 80):            # 主刻度：每 20 单位一条（深）
        x = img.width * v // 100
        y = img.height * v // 100
        od.line([(x, 0), (x, img.height)], fill=(0, 170, 0, 150), width=1)
        od.line([(0, y), (img.width, y)], fill=(0, 170, 0, 150), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(img)
    try:
        from PIL import ImageFont
        fnt = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
    except Exception:
        fnt = ImageFont.load_default()
    for v in (20, 40, 60, 80):
        d.text((img.width * v // 100 + 3, 3), str(v), fill=(30, 30, 30),
               font=fnt, stroke_width=2, stroke_fill=(255, 255, 255))
        d.text((3, img.height * v // 100 + 3), str(v), fill=(30, 30, 30),
               font=fnt, stroke_width=2, stroke_fill=(255, 255, 255))
    d.rectangle([0, img.height - 20, img.width, img.height], fill=(255, 255, 255))
    d.text((6, img.height - 17),
           f"第{row}行第{col}列格 放大{scale}倍  横轴=ox 纵轴=oy（左上0,0 右下100,100）",
           fill=(30, 30, 30), font=fnt)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    LAST_AIM["jpeg"] = buf.getvalue()
    LAST_AIM["cell"] = (box["id"], row, col)
    return (f"[瞄准] 已生成第{row}行第{col}列格的放大图（放大{scale}倍，随下条消息附上，"
            "横轴刻度=ox、纵轴刻度=oy）。请读出目标中心的 ox/oy，然后调用本工具并填全 "
            f"row/col/ox/oy 完成点击；不要重复瞄准同一格。")


def h_mem_save(args):
    rel = mem_save(str(args.get("folder", "")), str(args.get("filename", "")),
                   str(args.get("content", "")))
    return f"已保存记忆文件: {rel}"


def h_mem_list(args):
    tree = mem_tree()
    lines = [f"{f['name']}/ ({len(f['files'])}个文件)" for f in tree["folders"]]
    lines += [f"(根目录) {f['name']}" for f in tree["loose"]]
    return "记忆文件夹结构：\n" + ("\n".join(lines) if lines else "（记忆库还是空的）")


def h_mem_read(args):
    rel = str(args.get("path", ""))
    text = mem_read(rel)
    return f"{rel} 内容：\n{text}"


def h_mem_search(args):
    kw = str(args.get("keyword", "")).strip()
    if not kw:
        raise ValueError("keyword 为空")
    hits = mem_search(kw)
    return "\n".join(hits) if hits else f"没有找到包含「{kw}」的记忆"


def _S(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}

_ROW = {"type": "integer", "description": "行号1~10：绿色网格从上往下数的第几行（最上一行=1）"}
_COL = {"type": "integer", "description": "列号1~10：绿色网格从左往右数的第几列（最左一列=1）"}
_OX = {"type": "integer", "description": "格内水平偏移0~100：0=格左缘 50=居中 100=格右缘"}
_OY = {"type": "integer", "description": "格内垂直偏移0~100：0=格顶 50=居中 100=格底"}
_BID = {"type": "integer", "description": "监视框编号；只有一个框时可省略"}

TOOLS: dict[str, dict] = {
    "left_click": {"desc": "点击某格子内的目标：row/col 填绿色网格的格号（行从上往下数、列从左往右数），ox/oy 填格内偏移；小图标/看不准的目标可只填 row/col 先放大瞄准，看清后再填全点击",
                   "schema": _S({"box_id": _BID, "row": _ROW, "col": _COL,
                                 "ox": _OX, "oy": _OY}, ["row", "col"]),
                   "prompt": ("【适用】点击纯图标、无文字可点选的目标；带文字的按钮、会话名、菜单项一律改用 click_text，点名即中更准。\n"
                              "【两种用法（重要）】\n"
                              "A 大按钮、位置明确：一步点击——row/col/ox/oy 全填。\n"
                              "B 小图标（约 25px 以下）、一排密集图标、看不准的目标：两步走——第一步只填 row/col（不填 ox/oy）＝先瞄准，程序回传该格放大图（横轴刻度=ox、纵轴=oy，每 20 单位标数字）；第二步在放大图上读出目标中心的 ox/oy，四个参数填全再次调用＝真正点击。\n"
                              "【绿色网格怎么数】画面上叠加了绿色 10×10 定位格，不标任何数字，行列要自己数：横竖各有 9 条内部绿线，连同画面边界把画面分成 100 格。row＝从上往下数的第几行（最上一行=row1，最下一行=row10）；col＝从左往右数的第几列（最左一列=col1，最右一列=col10）。ox/oy 是格内偏移：格左上=0,0、格右下=100,100，正中约 50,50，对着主格线上的短刻度读（每 25 单位一个刻度）。\n"
                              "【压线与贴边】目标中心正好压在绿线上：压竖线＝归左格填 ox=100、或归右格填 ox=0（两种等价，选一种写）；压横线＝归上格填 oy=100、或归下格填 oy=0。目标贴画面边缘：最上＝row1 且 oy=0、最左＝col1 且 ox=0、最下＝row10 且 oy=100、最右＝col10 且 ox=100。数格子没把握时，先只填 row/col 瞄准，看放大图核对该格内容对不对。\n"
                              "【定位三步（先想后点）】1) 一句话描述目标外观（颜色/形状/含义）；2) 报出目标上下左右最近的参照物；3) 再按上面读坐标。\n"
                              "【调用示例】一步：{\"row\": 2, \"col\": 3, \"ox\": 50, \"oy\": 80}（多框时才需加 \"box_id\"）。先瞄准：{\"row\": 2, \"col\": 3} → 看放大图 → {\"row\": 2, \"col\": 3, \"ox\": 62, \"oy\": 38}。\n"
                              "【纪律】同一格不要连续瞄准两次；瞄准后必须填全参数完成点击；一步点击的会附落点放大图供核对，瞄准后点击的不重复附图（你刚看过）；点完看自动附上的最新截图核对效果。\n"
                              "【错误处理】row/col 不在 1~10 会被打回；只填 ox/oy 之一也会被打回；回执『命中窗口』不是目标程序＝点偏或被遮挡，禁止按原坐标连点。"),
                   "handler": h_click},
    "double_click": {"desc": "在监视框内双击（定位方式同 left_click）",
                     "schema": _S({"box_id": _BID, "row": _ROW, "col": _COL,
                                   "ox": _OX, "oy": _OY}, ["row", "col"]),
                     "prompt": ("【适用】需要双击才生效的目标：打开文件/文件夹、选中一段文字、双击列表项进详情。单击就行的场景不要用双击，避免误触。\n"
                                "【定位】与 left_click 完全相同：row/col+ox/oy 一步点击；小图标也可只填 row/col 先瞄准该格放大、看清后再填全参数双击；一步点击附落点放大图。\n"
                                "【调用示例】{\"row\": 1, \"col\": 2, \"ox\": 30, \"oy\": 50}。\n"
                                "【错误处理】回执『命中窗口』异常＝点偏，按放大图修正后重试；双击后画面无反应＝目标可能不支持双击，换单击或右键试，不要加倍次数。\n"
                                "【配合】点完看自动附上的新截图核对；文字目标优先 click_text。"),
                     "handler": h_double},
    "right_click": {"desc": "在监视框内右键单击（定位方式同 left_click）",
                    "schema": _S({"box_id": _BID, "row": _ROW, "col": _COL,
                                  "ox": _OX, "oy": _OY}, ["row", "col"]),
                    "prompt": ("【适用】调出右键菜单的场景：复制、粘贴、删除、属性等快捷操作。\n"
                               "【定位】与 left_click 相同（row/col+ox/oy；小图标也可只填 row/col 先瞄准放大），对准要右键的对象本体。\n【调用示例】{\"row\": 4, \"col\": 2, \"ox\": 50, \"oy\": 50}。\n"
                               "【典型流程】right_click 后画面出现菜单 → 用 click_text 点菜单项文字（如『复制』）；菜单项没文字才用 left_click 定位点。\n"
                               "【错误处理】菜单没弹出＝点偏了，按放大图修正后重试；弹出的菜单超出监视框看不到时如实告知用户，不要凭空猜菜单项。\n"
                               "【注意】菜单里点错可能触发删除等不可逆操作，动手前看清菜单项文字。"),
                    "handler": h_rclick},
    "drag": {"desc": "按住左键从起点格子内一点拖到终点格子内一点",
             "schema": _S({"box_id": _BID,
                           "row1": _ROW, "col1": _COL, "ox1": _OX, "oy1": _OY,
                           "row2": _ROW, "col2": _COL, "ox2": _OX, "oy2": _OY},
                          ["row1", "col1", "row2", "col2"]),
             "prompt": ("【适用】按住左键拖动：滑块（音量/进度）、移动窗口内元素、框选一段内容。\n"
                        "【参数】起点 row1/col1/ox1/oy1、终点 row2/col2/ox2/oy2，都是同一套『格号+格内偏移』定位（绿格计数与压线规则同 left_click）；起点放大图会随回执自动附上，可核对起点是否抓准。\n"
                        "【调用示例】{\"row1\": 3, \"col1\": 4, \"ox1\": 50, \"oy1\": 50, \"row2\": 3, \"col2\": 6, \"ox2\": 50, \"oy2\": 50} ＝ 从第3行第4列格子中心水平拖到第3行第6列格子中心。\n"
                        "【错误处理】回执『命中窗口』异常＝拖偏，按放大图修正起点后重试；程序是匀速快速拖动，若目标要求极慢拖拽（如验证滑块）可能失败，如实告知用户。\n"
                        "【配合】拖完看新截图核对位置；跨窗口、超出监视框范围的拖拽做不到，不要尝试。"),
             "handler": h_drag},
    "scroll": {"desc": "在指定位置滚动鼠标滚轮（每格约100像素；程序会自动重试并报告是否到底/到顶。读聊天记录等长内容建议配合 read_text 整屏转录，比反复滚动高效）",
               "schema": _S({"box_id": _BID, "row": _ROW, "col": _COL,
                             "ox": _OX, "oy": _OY,
                             "direction": {"type": "string",
                                           "enum": ["up", "down"]},
                             "amount": {"type": "integer",
                                        "description": "格数1~10，默认3"}},
                            ["direction"]),
               "prompt": ("【适用】滚动查看长内容（聊天记录、网页、列表）。direction 必填（up/down），amount 1~10，每格约 100 像素，默认 3。\n"
                          "【行为】程序先把鼠标移到 row/col/ox/oy 指定位置再滚；画面没动会自动补滚最多 3 次并在回执里说明。\n【调用示例】{\"direction\": \"down\", \"amount\": 3, \"row\": 5, \"col\": 5, \"ox\": 50, \"oy\": 50}。\n"
                          "【错误处理】回执『画面均无变化』＝已到顶/到底或该区域不可滚动，换方向试一次确认即可，不要反复滚。\n"
                          "【配合】读长文优先 read_screen 或 read_text 一次读完；滚动前确认鼠标位置是可滚动区域，别悬停在会弹预览的元素上。"),
               "handler": h_scroll},
    "get_focus": {"desc": "查询当前键盘焦点在哪个窗口、是否在监视框内（输入前确认用）",
                  "schema": _S({}, []),
                  "prompt": ("【适用】任何输入文字（type_text）之前确认键盘焦点在哪，避免打错窗口。\n"
                             "【参数】无。\n【调用示例】{}。\n"
                             "【读回执】『焦点在监视框内: 是，可以安全输入』→ 可以直接 type_text；『否——文字会打进这里！』→ 必须先用点击工具点中目标输入框，再 get_focus 复查，确认『是』后才输入。\n"
                             "【配合】标准输入流程：click_text/left_click 点输入框 → get_focus 确认 → type_text → press_keys enter。"),
                  "handler": h_focus},
    "click_text": {"desc": "按文字精确点击（带字目标首选，点名即中）：text=目标上的文字，如『椒椒』『发送』『公众号』；程序用本地 OCR 实时定位精确坐标",
                   "schema": _S({"text": {"type": "string",
                                          "description": "目标上显示的文字"},
                                 "nth": {"type": "integer",
                                         "description": "同名目标选第几个，默认1"}},
                                ["text"]),
                   "prompt": ("【适用】首选定位方式：目标上有文字就一律用它（按钮、会话名、菜单项、链接）。本地 OCR 实时定位，比坐标点击又快又准，天然不受画面时间差影响。\n"
                              "【参数】text 填画面上显示的原文（精确子串，如『发送』『椒椒』）；同名目标多处时用 nth 选第几个（默认第 1 个）。\n【调用示例】{\"text\": \"发送\"}；同名目标选第 2 个：{\"text\": \"椒椒\", \"nth\": 2}。\n"
                              "【错误处理】回执『没找到文字』时会列出当前可见文字清单——照清单里的实际写法重试（OCR 可能有细微出入）；目标确实不在画面里就如实说明，不要硬点。\n"
                              "【配合】右键弹出的菜单项优先用它点；纯图标目标才退回 left_click（自带落点放大图核对）；打开聊天也可以改按 ctrl+f 用名字搜索，不用肉眼找；点击后同样会收到新截图核对。"),
                   "handler": h_click_text},
    "read_text": {"desc": "本地 OCR 整屏精确转录：读出框内所有文字及其0~1000坐标（确定性、快、免估计；小字也能读）",
                  "schema": _S({"box_id": _BID}, []),
                  "prompt": ("【适用】需要精确原文和位置时：确认按钮文字、核对列表项、为点击取坐标。本地 OCR 逐字转录，快、准、确定性。\n"
                             "【返回格式】每行『(fx,fy) 文字』，fx/fy 是 0~1000 的框内坐标。\n【调用示例】{}。\n"
                             "【换算成点击参数】row = fy÷100 取整 +1；col = fx÷100 取整 +1；ox = fx 除以 100 的余数；oy = fy 除以 100 的余数。例：(235,410) → col=3、row=5、ox=35、oy=10。\n"
                             "【错误处理】识别不到文字＝画面被遮挡或纯图像；文字明显乱码＝截图瞬间窗口在动，重调一次通常恢复。\n"
                             "【配合】只要内容大意不要坐标时用 read_screen 更省；两者都失败时，可借 left_click 自带的落点放大图看清某个局部。"),
                  "handler": h_read_text},
    "clear_input": {"desc": "清空当前焦点输入框（ctrl+a + delete），输错位置时先点对输入框再用它清理",
                    "schema": _S({}, []),
                    "prompt": ("【适用】输错内容或输错位置后，清空当前焦点输入框（执行 ctrl+a 全选 + delete）。\n"
                               "【参数】无，作用于当前键盘焦点所在的输入框。\n【调用示例】{}。\n"
                               "【错误处理】焦点不在输入框时，ctrl+a 可能全选界面其它内容、delete 可能误删——清空前先 get_focus 确认焦点在输入框上。\n"
                               "【配合】标准纠错流程：点中正确的输入框 → clear_input 清掉错误内容 → type_text 重新输入。"),
                    "handler": h_clear},
    "read_screen": {"desc": "整屏读取并转录监视框内的文字（读聊天记录/长文本/列表用它，一次调用读全部，比反复滚动快得多）",
                    "schema": _S({"box_id": _BID,
                                  "question": {"type": "string",
                                               "description": "想从画面里了解什么，留空=逐字转录"}},
                                 []),
                    "prompt": ("【适用】读懂画面内容（聊天记录、长文本、列表、表格）：一次模型调用理解整屏，替代反复滚动翻找。带具体问题（question）更准更省，如『最后一条消息是谁发的、说了什么』；留空则逐字转录。\n【调用示例】{\"question\": \"最后一条消息是谁发的、说了什么\"}；逐字转录传 {}。\n"
                               "【与 read_text 的分工】read_screen 是模型理解性阅读，擅长总结、问答、理解上下文；read_text 是本地 OCR，擅长精确原文和坐标。理解内容用 read_screen，核对界面文字细节用 read_text。\n"
                               "【错误处理】返回空或『(无内容)』＝画面不可见，检查窗口是否被遮挡/最小化。\n"
                               "【配合】读完后要操作界面元素时，位置信息用 read_text 或 click_text 补齐。"),
                    "handler": h_read_screen},
    "type_text": {"desc": "向当前焦点位置输入文字（先点击目标输入框；焦点不在框内会被安全拦截）",
                  "schema": _S({"text": {"type": "string"},
                                "force": {"type": "boolean",
                                          "description": "确认无视焦点检查强制输入"}},
                               ["text"]),
                  "prompt": ("【适用】向输入框打字（聊天输入框、搜索框、表单）。原理是剪贴板粘贴，支持中文和多行。\n"
                             "【前置条件（硬性）】先用点击工具点中目标输入框；程序有焦点护栏，焦点不在监视框内会拒绝输入。\n【调用示例】{\"text\": \"你好，今晚一起吃饭吗？\"}。\n"
                             "【错误处理】回执『已阻止输入』＝当前焦点窗口不对，先点对输入框再输；确认就要输在当前焦点时才带 force:true 重试（慎用）。\n"
                             "【配合】标准流程：click_text 点输入框 → get_focus 确认 → type_text → 用户明确要求发送时再 press_keys enter；输错了先 clear_input。"),
                  "handler": h_type},
    "press_keys": {"desc": "按组合键，如 ctrl+c、enter、alt+tab",
                   "schema": _S({"keys": {"type": "string",
                                          "description": "如 ctrl+v"},
                                 "repeat": {"type": "integer",
                                            "description": "重复次数1~3，默认1（enter偶尔被应用吞掉时设2补一次）"}},
                                ["keys"]),
                   "prompt": ("【适用】按键和组合键：enter 发送、ctrl+f 搜索、ctrl+c/v 复制粘贴、esc 关弹窗、alt+tab 切窗口。\n"
                              "【参数】keys 用加号组合，如 \"ctrl+v\"、\"enter\"、\"ctrl+shift+esc\"；repeat 1~3，enter 偶尔被应用吞掉时设 2 补一次。\n【调用示例】{\"keys\": \"ctrl+v\"}；enter 被吞补按一次：{\"keys\": \"enter\", \"repeat\": 2}。\n"
                              "【错误处理】按键无反应＝焦点不在目标窗口，先点击目标窗口内元素再按；不要把 repeat 加到 3 以上。\n"
                              "【注意】enter/删除类按键可能触发不可逆操作（发送消息、删除内容），必须在用户明确要求后才按；发送后输入框清空即成功，气泡可能延迟 1~2 秒，切勿重复按 enter。"),
                   "handler": h_keys},
    "wait": {"desc": "等待页面响应（0.3~5 秒）",
             "schema": _S({"seconds": {"type": "number"}}, []),
             "prompt": ("【适用】等待界面响应：页面加载、动画播放、发送后气泡出现、搜索结果返回。范围 0.3~5 秒，默认 1。\n"
                        "【调用示例】{\"seconds\": 2}。\n"
                        "【说明】每次工具执行后程序本来就会等一拍并附新截图，wait 只用于明确知道『要等一会儿才有结果』的场景，不要滥用空等。\n"
                        "【配合】wait 后看自动附上的新截图再决策；长时间无变化说明可能加载失败，如实告知用户。"),
             "handler": h_wait},
    "memory_save": {"desc": "把原文保存为 markdown 记忆文件（按主题文件夹分类）",
                    "schema": _S({"folder": {"type": "string",
                                             "description": "分类文件夹，如 微信"},
                                  "filename": {"type": "string",
                                               "description": "文件名（自动加.md）"},
                                  "content": {"type": "string",
                                              "description": "markdown 原文"}},
                                 ["folder", "filename", "content"]),
                    "prompt": ("【适用】用户让记住信息、或画面出现值得长期保留的重要原文（地址、日程、账号、重要决定）时保存。\n"
                               "【参数】folder=主题分类（如 微信、网页、项目、日程，不存在会自动建）；filename=有意义的名字，最好带对象和日期（如 张三-2026-10-30，后缀自动补）；content=完整的 markdown 原文，写清来源和上下文。\n【调用示例】{\"folder\": \"微信\", \"filename\": \"张三-2026-11-02\", \"content\": \"## 张三\\n- 地址：上海市浦东新区XX路88号\\n- 生日：3月5日\"}。\n"
                               "【错误处理】回执带保存后的相对路径＝成功；文件名里的非法字符会被自动替换。\n"
                               "【原则】只存重要的、用户要求的内容，闲聊不存；同一主题的新信息新建带日期的文件，不覆盖旧文件。"),
                    "handler": h_mem_save},
    "memory_list": {"desc": "查看记忆库有哪些分类文件夹和文件",
                    "schema": _S({"folder": {"type": "string"}}, []),
                    "prompt": ("【适用】回忆或保存前先看记忆库结构：有哪些分类文件夹、各有多少文件。\n"
                               "【参数】无需填（固定返回全部结构）。\n【调用示例】{}。\n"
                               "【配合】标准回忆流程：memory_list 看分类 → memory_search 搜关键词 → memory_read 读全文；已经知道文件路径时可跳过前两步。"),
                    "handler": h_mem_list},
    "memory_read": {"desc": "读取某个记忆文件的内容",
                    "schema": _S({"path": {"type": "string",
                                           "description": "相对路径，如 微信/张三.md"}},
                                 ["path"]),
                    "prompt": ("【适用】读取某个记忆文件的完整内容（超长会截断到约 6000 字）。\n"
                               "【参数】path=相对路径，如 \"微信/张三-2026-10-30.md\"，来自 memory_list 或 memory_search 的结果。\n【调用示例】{\"path\": \"微信/张三-2026-11-02.md\"}。\n"
                               "【错误处理】报『路径不合法/不存在』＝路径写错，回 memory_list 核对真实文件名再读。\n"
                               "【配合】读到后按用户问题作答，不要整段原文背诵给用户；需要补充或更新时用 memory_save 写带新日期的文件。"),
                    "handler": h_mem_read},
    "memory_search": {"desc": "在全部记忆文件中搜索关键词",
                      "schema": _S({"keyword": {"type": "string"}}, ["keyword"]),
                      "prompt": ("【适用】按关键词找记忆：先搜后读，不用翻遍所有文件。\n"
                                 "【参数】keyword=精确子串匹配（不是语义搜索）。搜不到时用更短的词、试不同写法（如搜『地址』没有就试『路』『号楼』）。\n【调用示例】{\"keyword\": \"地址\"}。\n"
                                 "【返回】匹配的文件名和内容摘要片段。\n"
                                 "【错误处理】『没有找到』＝换关键词或先 memory_list 看分类；确认库里没有就如实告知用户。\n"
                                 "【配合】search 拿到路径 → memory_read 读全文。"),
                      "handler": h_mem_search},
}


def enabled_tool_schemas() -> list[dict]:
    """发给模型的工具定义：desc 是一句话简介，prompt 是详细使用说明，
    两者合并成 description 随工具定义自动注入（LangChain/LangGraph 同款机制）。"""
    tcfg = CONFIG.get("tools") or {}
    out = []
    for n, t in TOOLS.items():
        if not tcfg.get(n, True):
            continue
        desc = t["desc"]
        if t.get("prompt"):
            desc += "\n【使用说明】\n" + t["prompt"]
        out.append({"type": "function",
                    "function": {"name": n, "description": desc,
                                 "parameters": t["schema"]}})
    return out


# 会改变屏幕状态的工具：执行后需要立刻重新截图给 AI 核对效果
INPUT_TOOLS = {"left_click", "double_click", "right_click", "drag",
               "scroll", "type_text", "press_keys", "wait", "clear_input",
               "click_text"}
# 输入类工具：执行后界面渲染有延迟（约1秒），截图前需要等 settle 时间
SETTLE_TOOLS = {"type_text", "press_keys", "clear_input"}
# 屏幕动作类工具：一轮只执行一个，执行后立刻截图核对（sense-act-verify 标准循环）
COORD_ACTIONS = {"left_click", "double_click", "right_click", "drag", "scroll",
                 "click_text"}


def execute_tool(name: str, args: dict) -> str:
    tool = TOOLS.get(name)
    if tool is None:
        return f"未知工具: {name}"
    if not (CONFIG.get("tools") or {}).get(name, True):
        return f"工具 {name} 已被用户在「大脑」页关闭"
    try:
        return str(tool["handler"](args))
    except Exception as e:
        return f"工具 {name} 执行失败: {type(e).__name__}: {e}"


# ================================================================ 会话与上下文
def _history_file() -> Path:
    """会话历史文件：目录可在 config 的 history_dir 里改，默认项目 data 目录。"""
    return Path(CONFIG.get("history_dir") or DATA_DIR) / "history.json"


def _migrate_history_file() -> None:
    """老版本会话固定存在 data/history.json；换了位置后第一次启动自动搬过去。"""
    old = DATA_DIR / "history.json"
    new = _history_file()
    try:
        if new.resolve() == old.resolve() or not old.exists():
            return
        if not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
            print(f"[会话] 历史记录已迁移到: {new}")
    except Exception:
        pass


_migrate_history_file()


def _historys_load() -> dict[str, dict]:
    """会话结构：{名字: {"summary": 滚动摘要, "messages": [原文消息]}}"""
    try:
        data = json.loads(_history_file().read_text(encoding="utf-8"))
        out = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):   # 旧版：纯消息列表
                    out[k] = {"summary": "",
                              "messages": [m for m in v if isinstance(m, dict)]}
                elif isinstance(v, dict) and isinstance(v.get("messages"), list):
                    out[k] = {"summary": str(v.get("summary") or ""),
                              "messages": [m for m in v["messages"]
                                           if isinstance(m, dict)]}
            return out
        if isinstance(data, list):   # 更旧版：单列表
            legacy = [m for m in data
                      if isinstance(m, dict)
                      and m.get("role") in ("user", "assistant")
                      and isinstance(m.get("content"), str)]
            if legacy:
                return {"默认会话": {"summary": "", "messages": legacy}}
    except Exception:
        pass
    return {}


def _historys_save() -> None:
    try:
        f = _history_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(HISTORYS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


HISTORYS: dict[str, dict] = _historys_load() or {
    "默认会话": {"summary": "", "messages": []}}
HISTORYS.setdefault("默认会话", {"summary": "", "messages": []})
CONTEXT_RUNS: deque = deque(maxlen=8)   # 每轮任务的完整上下文快照（截图用占位符表示）


def est_tokens(text: str) -> int:
    """粗估 token 数：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    text = str(text or "")
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return cjk + max(1, (len(text) - cjk) // 4)


def _history_tokens(sess: dict) -> int:
    t = est_tokens(sess.get("summary") or "")
    for m in sess.get("messages", []):
        t += est_tokens(m.get("content") or "")
    return t


def maybe_compress(session: str) -> None:
    """上下文超过预算时：把较旧对话压缩成滚动摘要，保留最近几条原文。

    即业界通行的 ConversationSummaryBufferMemory / auto-compact 模式。
    """
    if STATE["stop"] or STATE["paused"]:
        return
    sess = HISTORYS.get(session)
    if not sess:
        return
    budget = int(CONFIG.get("context_budget", 800000))
    if _history_tokens(sess) <= int(budget * 0.8):
        return
    keep = max(4, int(CONFIG.get("compress_keep_recent", 8)))
    msgs = sess["messages"]
    if len(msgs) <= keep:
        return
    old, recent = msgs[:-keep], msgs[-keep:]
    prompt = (
        "请把下面这段人机对话合并压缩成一份要点摘要，供 AI 作为长期记忆使用。\n"
        "必须保留：用户的身份与偏好、重要决定、未完成的任务、关键事实与数字。\n"
        "用紧凑的中文要点列出，不要客套话，不要评论。\n\n"
        f"【现有摘要】\n{sess.get('summary') or '（无）'}\n\n"
        "【要压缩进去的对话】\n"
        + "\n".join(f"{m.get('role')}: {str(m.get('content'))[:2000]}"
                    for m in old)[:60000])
    try:
        r = GLM_CLIENT.chat([{"role": "user", "content": prompt}])
        new_summary = (r.get("content") or "").strip()
        if new_summary:
            sess["summary"] = new_summary
            sess["messages"] = recent
            _historys_save()
            add_step("compress",
                     f"上下文压缩完成：摘要 {len(new_summary)} 字，"
                     f"保留最近 {len(recent)} 条原文")
            log_event(f"会话「{session}」上下文已压缩")
    except Exception as e:
        log_event(f"上下文压缩失败（下轮重试）: {e}")


# 声称"已执行动作"的关键词：出现这些但本轮零工具调用 = 编造回执
_ACTION_WORDS = ("已点击", "点击了", "已发送", "发送成功", "发送了", "已回复",
                 "回复了", "已输入", "输入了", "已打开", "打开了", "已切换",
                 "切换到", "已滚动", "滚动了", "已清空", "已代回", "代回了",
                 "已发起", "已按序")


def _claims_action(text: str) -> bool:
    return any(k in text for k in _ACTION_WORDS)


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """把模型真正收到的消息整理成可展示的快照：图片替换为[截图]占位。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    parts.append("[截图]")
            c = "\n".join(parts)
        tcs = None
        if m.get("tool_calls"):
            tcs = [f"{tc['function']['name']}({str(tc['function'].get('arguments'))[:100]})"
                   for tc in m["tool_calls"]]
        out.append({"role": m.get("role"),
                    "content": str(c or "")[:1500],
                    "tool_calls": tcs})
    return out


def _shots_content(trigger: str) -> list[dict] | None:
    """把当前各框画面打包成消息 content parts；无画面则返回 None。"""
    shots = [(b["name"], b["shot"], tuple(b.get("served_size") or (0, 0)))
             for b in BOXES.values() if b.get("shot")]
    if not shots:
        return None
    parts = [{"type": "text", "text": trigger}]
    for name, jpeg, (sw, sh) in shots:
        b64 = base64.b64encode(jpeg).decode()
        parts.append({"type": "text",
                      "text": f"[监视框「{name}」的画面：原图 {sw}×{sh} 像素，"
                              "已叠加绿色 10×10 定位格（不标数字：行从上往下数 1~10、"
                              "列从左往右数 1~10，计数与压线规则见 left_click 工具说明），"
                              f"每格约 {sw // 10}×{sh // 10} 像素]"})
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/png;base64," + b64
                                    if jpeg[:8] == b"\x89PNG\r\n\x1a\n"
                                    else "data:image/jpeg;base64," + b64}})
    return parts


def agent_turn(trigger_text: str, session: str | None = None,
               store_text: str | None = None) -> str:
    """跑一轮完整 agent（视觉->决策->工具->...->结论）。

    聊天、画面监控、看一眼全部走同一个当前会话：
    用户在聊天里交代的规则，监控执行时同样可见、必须遵守。
    """
    session = session or CONFIG.get("active_session") or "默认会话"
    if STATE["paused"]:
        raise RuntimeError("AI 已暂停：任何消息都无法唤醒，请先关闭「暂停 AI」开关")
    with _agent_lock:
        if STATE["busy"]:
            raise RuntimeError("AI 正在执行中，请等它完成或点「停止」")
        STATE["busy"] = True
        STATE["stop"] = False
    try:
        return _agent_run(trigger_text, store_text, session)
    finally:
        STATE["busy"] = False


def _agent_run(trigger_text: str, store_text: str | None, session: str) -> str:
    STATS["runs"] += 1
    sys_prompt = (CONFIG["system_prompt"] or DEFAULT_PROMPT).replace(
        "{memory_dir}", CONFIG["memory_dir"])
    sess = HISTORYS.setdefault(session, {"summary": "", "messages": []})
    if CONFIG.get("dry_run"):
        sys_prompt += ("\n\n⚠️ 当前处于演练模式：所有屏幕操作都不会真正执行，"
                       "只回显将要进行的动作。")
    maybe_compress(session)   # 开跑前若超预算，先压缩历史
    hist: list[dict] = []
    if sess.get("summary"):
        hist.append({"role": "user",
                     "content": "[此前对话的压缩摘要，供参考]\n" + sess["summary"]})
    hist += list(sess["messages"])
    messages: list[dict] = [{"role": "system", "content": sys_prompt}]
    messages += hist
    content = _shots_content(trigger_text)
    if content is not None:
        messages.append({"role": "user", "content": content})
    else:
        note = ""
        if BOXES:
            note = ("\n\n[系统提示：当前没有任何可用的监视框画面——窗口可能被遮挡、"
                    "最小化或已关闭。不要描述画面内容，也不要执行屏幕操作，"
                    "请如实告知用户。]")
        messages.append({"role": "user", "content": trigger_text + note})
    add_step("trigger", trigger_text[:150])

    tools = enabled_tool_schemas()
    limit = int(CONFIG.get("max_steps", 12))
    final = ""
    turn_tools = 0   # 本轮真实执行的工具次数
    forced_left = 2  # 防编造：最多强制重试 2 次
    for _round in range(limit):
        if STATE["stop"]:
            final = "已被用户停止。"
            add_step("error", final)
            break
        try:
            msg = GLM_CLIENT.chat(messages, tools=tools)
        except Exception as e:
            STATS["errors"] += 1
            if STATE["stop"] or STATE["paused"]:
                final = "已被用户停止。"
            else:
                final = f"模型调用失败: {e}"
            add_step("error", final)
            log_event(final)
            break
        if msg["tool_calls"]:
            if msg["content"]:
                add_step("thought", msg["content"])
            messages.append({"role": "assistant", "content": msg["content"] or "",
                             "tool_calls": [
                                 {"id": c["id"], "type": "function",
                                  "function": {"name": c["name"],
                                               "arguments": c["args"]}}
                                 for c in msg["tool_calls"]]})
            used_input = False
            needs_settle = False
            coord_done = False   # 业界标准（Anthropic/OpenAI）：一轮只做一个屏幕动作，
            coord_skipped = []   # 做完立刻看新画面，再做下一个（sense-act-verify）
            first_in_round = True
            for c in msg["tool_calls"]:
                if STATE["stop"]:
                    break
                if not first_in_round:
                    time.sleep(0.1)   # 相邻工具间留一拍，应用跟得上按键/焦点切换
                first_in_round = False
                try:
                    args = json.loads(c["args"])
                except Exception:
                    args = {}
                aim_only = (c["name"] in ("left_click", "double_click", "right_click")
                            and isinstance(args, dict)
                            and args.get("ox") is None and args.get("oy") is None)
                if c["name"] in COORD_ACTIONS and coord_done and not aim_only:
                    out = ("已跳过：一轮只执行一个屏幕操作。上一个操作已完成，"
                           "上方已附最新画面；请基于它确认效果后，再重新发起这个操作。")
                    coord_skipped.append(c["name"])
                else:
                    out = execute_tool(c["name"], args if isinstance(args, dict) else {})
                    if c["name"] in COORD_ACTIONS:
                        coord_done = True
                    STATS["tool_calls"] += 1
                    turn_tools += 1
                    if c["name"] in INPUT_TOOLS and not aim_only:
                        used_input = True   # 瞄准不点击：不算屏幕输入，无需等渲染再截图
                    if c["name"] in SETTLE_TOOLS:
                        needs_settle = True
                add_step("tool", f"{c['name']}({', '.join(f'{k}={v}' for k, v in list(args.items())[:4])}) => {out[:160]}")
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": out[:4000]})
            if used_input:
                # 操作后界面有动画/渲染延迟：先等一拍再截图，
                # 否则 AI 看到的是动画中途的"半成品画面"（误判+过期防护误拦）
                time.sleep(1.2 if needs_settle else 0.7)
                shots = _shots_content(
                    "以上工具刚执行完毕，这是各监视框此刻的最新画面。请完成三件事：\n"
                    "1. 核对：你刚才的操作是否生效、结果是否符合预期；"
                    "没有生效或位置不对就调整后重试；\n"
                    "2. 观察：画面里是否出现了与你的操作无关的新变化"
                    "（例如别人发来的新消息、内容自动刷新、弹窗、状态更新），"
                    "重要的信息按记忆规则留存；\n"
                    "3. 决策：综合以上两点决定下一步——继续原计划、调整操作，"
                    "还是先处理新出现的情况。")
                if shots:
                    messages.append({"role": "user", "content": shots})
            if LAST_ZOOM.get("jpeg"):
                zb64 = base64.b64encode(LAST_ZOOM.pop("jpeg")).decode()
                messages.append({"role": "user", "content": [
                    {"type": "text",
                     "text": "这是你刚才那次点击/拖拽起点的落点放大图：红十字中心＝实际落点，"
                             "图上标注了区域四角与十字的 0~1000 坐标。"
                             "请核对十字是否正中目标：偏离就按图上坐标估算偏差，"
                             "修正 row/col/ox/oy 后重新操作；已正中且结果符合预期就继续下一步。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{zb64}"}},
                ]})
            if LAST_AIM.get("jpeg"):
                ab64 = base64.b64encode(LAST_AIM.pop("jpeg")).decode()
                messages.append({"role": "user", "content": [
                    {"type": "text",
                     "text": "这是你刚才要求瞄准的格子放大图：横轴刻度＝ox、纵轴刻度＝oy"
                             "（左上 0,0、右下 100,100，每 20 单位标了数字）。"
                             "在图上找到目标中心，读出它的 ox/oy，然后用同一个点击工具把 "
                             "row/col/ox/oy 填全再次调用，即可完成点击；"
                             "目标不在这一格就换个格重新瞄准。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{ab64}"}},
                ]})
            continue
        cand = (msg["content"] or "").strip()
        # 防编造校验（业界结构化强制）：声称执行了操作但本轮零工具调用 → 打回重做
        if cand and turn_tools == 0 and _claims_action(cand) \
                and forced_left > 0 and not STATE["stop"]:
            forced_left -= 1
            add_step("error", "检测到回复声称执行了操作，但没有任何真实工具调用，已打回要求真实执行")
            log_event("检测到编造的工具回执，强制要求真实执行")
            messages.append({"role": "user", "content":
                "你刚才的回复声称执行了操作，但本轮没有任何真实的工具调用记录——"
                "这是严格禁止的。请现在真实调用工具完成任务；"
                "若确实无需任何操作，回复「无需操作」四个字。"})
            continue
        final = cand
        add_step("final", final)
        break
    else:
        final = "已达单次执行步数上限，先停在这里。可在「大脑」页调大步数。"
        add_step("error", final)

    if final and not STATE["stop"]:
        sess["messages"].append({"role": "user",
                                 "content": store_text or trigger_text})
        sess["messages"].append({"role": "assistant", "content": final})
        _historys_save()
        maybe_compress(session)   # 追加后若超预算，立刻压缩
    CONTEXT_RUNS.append({"t": time.strftime("%m-%d %H:%M:%S"),
                         "session": session,
                         "messages": _sanitize_messages(messages)})
    log_event("本轮完成: " + (final or "(无输出)")[:80])
    return final or "(无输出)"


# ================================================================ 监控线程
def monitor_loop() -> None:
    while True:
        time.sleep(float(CONFIG.get("check_interval", 1.0)))
        # 所有框持续截图：预览保持实时，不随自动执行开关/AI 忙碌而停
        for box in list(BOXES.values()):
            score = capture_box(box)
            # 下面只控制"是否自动唤起 AI"，与截图解耦；
            # 暂停开关打开时，任何变化都不能唤醒 AI
            if not box.get("monitor"):
                continue
            if STATE["paused"] or not CONFIG.get("auto_run") or STATE["busy"]:
                continue
            if score is None:
                continue
            if score < float(CONFIG.get("change_threshold", 6)):
                box["hits"] = 0   # 降噪：需连续两拍超阈值才触发，过滤光标闪烁等抖动
                continue
            box["hits"] = box.get("hits", 0) + 1
            if box["hits"] < 2:
                continue
            box["hits"] = 0
            now = time.time()
            if now - box.get("last_trigger", 0) < float(
                    CONFIG.get("trigger_cooldown", 20)):
                continue
            box["last_trigger"] = now
            box["last_change"] = time.strftime("%H:%M:%S")
            log_event(f"框「{box['name']}」画面变化（{score:.1f}），唤起 AI")
            try:
                agent_turn(
                    f'监视框「{box["name"]}」画面发生了变化（变化度 {score:.1f}）。'
                    "请查看下面的最新画面，并结合此前对话中用户交代给你的规则来判断："
                    "如果画面里正好出现了用户让你处理的情况"
                    "（例如交代过有人发消息要回复，而画面里确实有新消息），"
                    "就按用户的交代去处理；其他需要处理的内容同样处理，"
                    "重要信息按规则存入记忆；若对照后确实无必要操作，简短说明即可。",
                    store_text=f'[自动] 监视框「{box["name"]}」画面变化（{score:.1f}）')
            except Exception as e:
                log_event(f"AI 执行失败: {e}")


def _mask_key(k: str) -> str:
    """api_key 掩码：只露头尾几位，供前端设置弹窗确认当前用的是哪把钥匙。"""
    k = str(k or "")
    if not k:
        return ""
    if len(k) <= 8:
        return k[:2] + "****"
    return k[:4] + "****" + k[-4:]


# ================================================================ HTTP
def list_windows() -> list[dict]:
    if win32gui is None:
        return []
    out: list[dict] = []

    def cb(h, _):
        if not (win32gui.IsWindowVisible(h) and win32gui.IsWindowEnabled(h)):
            return
        t = (win32gui.GetWindowText(h) or "").strip()
        if not t or len(t) < 2:
            return
        skip = ("Program Manager", "Windows 输入体验", "Microsoft Text Input",
                "Settings", "NVIDIA", "开始")
        if any(s.lower() in t.lower() for s in skip):
            return
        try:
            r = win32gui.GetClientRect(h)
            if r[2] < 200 or r[3] < 150:
                return
        except Exception:
            return
        out.append({"hwnd": h, "title": t[:70]})
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, (BASE / "ui.html").read_bytes(), "text/html")
            if u.path == "/api/status":
                return self._status()
            if u.path == "/api/windows":
                return self._json({"windows": list_windows()})
            if u.path == "/api/frames":
                return self._json({str(b["id"]): int(b.get("frame") or 0)
                                   for b in BOXES.values()})
            if u.path == "/api/thinking":
                return self._json({"text": STATE.get("thinking", ""),
                                   "busy": STATE["busy"]})
            if u.path == "/api/sessions":
                return self._json({"sessions": list(HISTORYS.keys()),
                                   "active": CONFIG.get("active_session",
                                                        "默认会话")})
            if u.path == "/api/history":
                s = q.get("session", ["默认会话"])[0]
                sess = HISTORYS.get(s, {"summary": "", "messages": []})
                return self._json({"session": s,
                                   "summary": sess.get("summary", ""),
                                   "tokens": _history_tokens(sess),
                                   "messages": sess.get("messages", [])})
            if u.path == "/api/context":
                runs = list(CONTEXT_RUNS)
                if not runs:
                    return self._json({"runs": [], "run": None})
                try:
                    idx = int(q.get("i", [len(runs) - 1])[0])
                except ValueError:
                    idx = len(runs) - 1
                idx = max(0, min(len(runs) - 1, idx))
                return self._json({
                    "latest": len(runs) - 1,
                    "runs": [{"i": i, "t": r["t"], "session": r["session"],
                              "n": len(r["messages"])}
                             for i, r in enumerate(runs)],
                    "run": runs[idx]})
            if u.path == "/api/box/shot":
                b = box_by_id(q.get("id", [""])[0])
                if b and b.get("shot"):
                    return self._send(200, b["shot"],
                                      b.get("shot_mime", "image/png"))
                return self._json({"error": "暂无画面"}, 404)
            if u.path == "/api/memory/tree":
                return self._json(mem_tree())
            if u.path == "/api/memory/file":
                rel = q.get("path", [""])[0]
                try:
                    return self._json({"path": rel, "content": mem_read(rel)})
                except Exception as e:
                    return self._json({"error": str(e)}, 400)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        body = self._body()
        try:
            if self.path == "/api/pick_region":
                try:
                    r = subprocess.run(
                        [sys.executable, str(PICKER)],
                        capture_output=True, text=True,
                        encoding="utf-8", timeout=180)
                    data = json.loads((r.stdout or "{}").strip() or "{}")
                    if data.get("region"):
                        return self._add_box({"mode": "region", "rect": data["region"]})
                    return self._json({"added": False, "note": "已取消框选"})
                except subprocess.TimeoutExpired:
                    return self._json({"error": "框选超时"}, 400)

            if self.path == "/api/box/add":
                return self._add_box(body)

            if self.path == "/api/box/del":
                b = box_by_id(body.get("id"))
                if b:
                    BOXES.pop(b["id"], None)
                    save_boxes()
                    return self._json({"deleted": True})
                return self._json({"error": "找不到该框"}, 404)

            if self.path == "/api/box/toggle":
                b = box_by_id(body.get("id"))
                if not b:
                    return self._json({"error": "找不到该框"}, 404)
                b["monitor"] = bool(body.get("enabled"))
                if b["monitor"]:
                    capture_box(b)
                save_boxes()
                return self._json({"ok": True, "monitor": b["monitor"]})

            if self.path == "/api/box/look":
                b = box_by_id(body.get("id"))
                if not b:
                    return self._json({"error": "找不到该框"}, 404)
                capture_box(b)
                reply = agent_turn(
                    f'用户让你看一眼监视框「{b["name"]}」的当前画面。'
                    "请描述画面里的关键内容（不要执行任何操作）。",
                    store_text=f'用户让看一眼「{b["name"]}」的画面')
                return self._json({"reply": reply})

            if self.path == "/api/chat":
                text = str(body.get("message", "")).strip()
                if not text:
                    return self._json({"error": "消息不能为空"}, 400)
                reply = agent_turn(text)
                return self._json({"reply": reply})

            if self.path == "/api/model/test":
                # 用弹窗里填的候选值发一条最小请求（未保存也可先测）
                ak = str(body.get("api_key") or "").strip() or CONFIG.get("api_key", "")
                bu = str(body.get("base_url") or "").strip() or CONFIG.get("base_url", "")
                md = str(body.get("model") or "").strip() or CONFIG.get("model", "")
                if not (ak and bu and md):
                    return self._json({"error": "模型 / 接口地址 / 密钥都不能为空"}, 400)
                try:
                    from openai import OpenAI
                    c = OpenAI(api_key=ak, base_url=bu, timeout=30)
                    r = c.chat.completions.create(
                        model=md, max_tokens=16,
                        messages=[{"role": "user", "content": "请只回复两个字母：OK"}])
                    reply = str((r.choices[0].message.content or "").strip()[:50])
                    return self._json({"ok": True,
                                       "reply": reply or "连接成功（模型无文本回复）"})
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 400)

            if self.path == "/api/session/new":
                name = str(body.get("name", "")).strip()
                if not name:
                    name = f"会话{len(HISTORYS) + 1}"
                HISTORYS.setdefault(name, {"summary": "", "messages": []})
                CONFIG["active_session"] = name
                save_config()
                _historys_save()
                return self._json({"ok": True, "name": name})

            if self.path == "/api/session/del":
                name = str(body.get("name", ""))
                if name == "默认会话":
                    return self._json({"error": "默认会话不能删除"}, 400)
                if name in HISTORYS and len(HISTORYS) > 1:
                    HISTORYS.pop(name)
                    if CONFIG.get("active_session") == name:
                        CONFIG["active_session"] = "默认会话"
                    save_config()
                    _historys_save()
                    return self._json({"ok": True})
                return self._json({"error": "会话不存在"}, 404)

            if self.path == "/api/session/rename":
                old = str(body.get("old", "")).strip()
                name = str(body.get("name", "")).strip()
                if old not in HISTORYS:
                    return self._json({"error": "会话不存在"}, 404)
                if not name:
                    return self._json({"error": "新名称不能为空"}, 400)
                if name == old:
                    return self._json({"ok": True, "name": name})
                if name in HISTORYS:
                    return self._json({"error": "已有同名会话"}, 400)
                HISTORYS[name] = HISTORYS.pop(old)
                if CONFIG.get("active_session") == old:
                    CONFIG["active_session"] = name
                save_config()
                _historys_save()
                log_event(f"会话已重命名: {old} → {name}")
                return self._json({"ok": True, "name": name})

            if self.path == "/api/session/active":
                name = str(body.get("name", "")).strip()
                if name not in HISTORYS:
                    return self._json({"error": "会话不存在"}, 404)
                CONFIG["active_session"] = name
                save_config()
                log_event(f"当前会话已切换: {name}")
                return self._json({"ok": True, "active": name})

            if self.path == "/api/exit":
                # 结束整个程序：先回复前端、把会话落盘，再退出进程
                def _bye():
                    try:
                        _historys_save()
                    except Exception:
                        pass
                    os._exit(0)
                threading.Timer(0.4, _bye).start()
                log_event("收到结束指令，程序即将退出")
                return self._json({"ok": True})

            if self.path == "/api/pause":
                STATE["paused"] = bool(body.get("paused"))
                if STATE["paused"]:
                    # 彻底停下：中断当前请求 + 打上闩，任何变化都不能再唤醒
                    STATE["stop"] = True
                    GLM_CLIENT.abort()
                    log_event("AI 已暂停（当前调用已中断，新的变化不再唤醒）")
                else:
                    STATE["stop"] = False
                    log_event("AI 已恢复")
                return self._json({"ok": True, "paused": STATE["paused"]})

            if self.path == "/api/config":
                return self._save_config(body)

            if self.path == "/api/memory/save":
                rel = mem_save(str(body.get("folder", "")),
                               str(body.get("filename", "")),
                               str(body.get("content", "")))
                return self._json({"saved": rel})
            if self.path == "/api/memory/delete":
                return self._json({"deleted": mem_delete(str(body.get("path", "")))})
            if self.path == "/api/memory/open":
                os.startfile(str(mem_root()))
                return self._json({"ok": True})
            if self.path == "/api/history/open":
                hd = Path(CONFIG.get("history_dir") or DATA_DIR)
                hd.mkdir(parents=True, exist_ok=True)
                os.startfile(str(hd))
                return self._json({"ok": True})

            return self._json({"error": "not found"}, 404)
        except RuntimeError as e:   # busy
            return self._json({"error": str(e)}, 409)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------- handlers ----------------
    def _add_box(self, body):
        mode = body.get("mode")
        if mode == "window":
            hwnd = int(body.get("hwnd") or 0)
            if win32gui is None or not win32gui.IsWindow(hwnd):
                return self._json({"error": "窗口句柄无效"}, 400)
            title = (win32gui.GetWindowText(hwnd) or "窗口")[:30]
            name = f'框{_next_box_id[0]}·{title[:12]}'
        elif mode == "region":
            r = body.get("rect")
            if not (isinstance(r, list) and len(r) == 4):
                return self._json({"error": "区域不合法"}, 400)
            name = f'框{_next_box_id[0]}·区域'
            title = ""
            hwnd = 0
        else:
            return self._json({"error": "mode 必须是 window 或 region"}, 400)
        box = {"id": _next_box_id[0], "name": name, "mode": mode,
               "hwnd": hwnd, "title": title,
               "rect": [int(v) for v in body["rect"]] if mode == "region" else None,
               "monitor": True,
               "shot": None, "prev_small": None, "last_trigger": 0,
               "last_change": "", "error": ""}
        _next_box_id[0] += 1
        BOXES[box["id"]] = box
        capture_box(box)
        save_boxes()
        log_event(f'新增{name}（{mode}）')
        return self._json({"added": True, "id": box["id"]})

    def _save_config(self, body):
        changed = []
        allowed = {"system_prompt", "memory_dir", "history_dir", "auto_run",
                   "dry_run", "check_interval", "change_threshold",
                   "trigger_cooldown", "max_steps", "context_budget",
                   "compress_keep_recent", "thinking"}
        for k in allowed:
            if k in body:
                CONFIG[k] = body[k]
                changed.append(k)
        # 模型接入配置（首页「设置」弹窗保存走这里）：留空 = 不修改
        rebind = False
        for k in ("model", "base_url"):
            v = str(body.get(k) or "").strip()
            if v and CONFIG.get(k) != v:
                CONFIG[k] = v
                changed.append(k)
                if k == "base_url":
                    rebind = True
        ak = str(body.get("api_key") or "").strip()
        if ak and ak != CONFIG.get("api_key"):
            CONFIG["api_key"] = ak
            changed.append("api_key")
            rebind = True
        if rebind:
            GLM_CLIENT.abort()   # 关闭旧连接：下一次调用按新密钥/地址重建
        if isinstance(body.get("tools"), dict):
            cur = CONFIG.setdefault("tools", {})
            for k, v in body["tools"].items():
                if k in TOOLS:
                    cur[k] = bool(v)
            changed.append("tools")
        if str(body.get("memory_dir", "")).strip():
            try:
                mem_root()
            except Exception as e:
                return self._json({"error": f"记忆目录不可用: {e}"}, 400)
        if str(body.get("history_dir", "")).strip():
            try:
                Path(CONFIG["history_dir"]).mkdir(parents=True, exist_ok=True)
                _historys_save()   # 立即把当前会话写进新位置；旧文件原地保留作备份
            except Exception as e:
                return self._json({"error": f"会话目录不可用: {e}"}, 400)
        save_config()
        log_event("设置已更新: " + "、".join(changed) if changed else "设置无变化")
        return self._json({"saved": True})

    def _status(self):
        boxes = []
        for b in BOXES.values():
            boxes.append({"id": b["id"], "name": b["name"], "mode": b["mode"],
                          "title": b.get("title", ""), "monitor": b.get("monitor"),
                          "rect": b.get("rect"), "error": b.get("error", ""),
                          "last_change": b.get("last_change", ""),
                          "frame": int(b.get("frame") or 0),
                          "has_shot": bool(b.get("shot"))})
        tcfg = CONFIG.get("tools") or {}
        with _lock:
            steps = list(STEPS)[-60:]
            events = list(EVENTS)[-14:]
        mem_ok = True
        try:
            mem_root()
        except Exception:
            mem_ok = False
        self._json({
            "model": CONFIG["model"],
            "base_url": CONFIG.get("base_url", ""),
            "api_ready": bool(CONFIG.get("api_key")),
            "api_key_masked": _mask_key(CONFIG.get("api_key", "")),
            "auto_run": CONFIG.get("auto_run", True),
            "dry_run": CONFIG.get("dry_run", False),
            "busy": STATE["busy"],
            "paused": STATE["paused"],
            "active_session": CONFIG.get("active_session", "默认会话"),
            "stats": STATS,
            "boxes": boxes,
            "steps": steps,
            "events": events,
            "memory_dir": CONFIG["memory_dir"],
            "memory_ok": mem_ok,
            "history_dir": str(CONFIG.get("history_dir") or DATA_DIR),
            "system_prompt": CONFIG.get("system_prompt", DEFAULT_PROMPT),
            "tools": [{"name": n, "desc": t["desc"], "prompt": t.get("prompt", ""),
                       "enabled": bool(tcfg.get(n, True))} for n, t in TOOLS.items()],
            "params": {"max_steps": CONFIG.get("max_steps", 12),
                       "change_threshold": CONFIG.get("change_threshold", 6),
                       "trigger_cooldown": CONFIG.get("trigger_cooldown", 20),
                       "check_interval": CONFIG.get("check_interval", 1.0),
                       "context_budget": CONFIG.get("context_budget", 800000),
                       "compress_keep_recent":
                           CONFIG.get("compress_keep_recent", 8),
                       "thinking": CONFIG.get("thinking", "enabled")},
        })


# ================================================================ 入口
def main() -> None:
    if not CONFIG.get("api_key"):
        print("[错误] config.json 里没有 api_key")
        sys.exit(1)
    load_boxes()
    mem_root()
    port = int(CONFIG.get("web_port", 8770))
    ThreadingHTTPServer.allow_reuse_address = False   # 端口被占时直接报错，避免新旧进程抢答
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[错误] 端口 {port} 被占用（可能上一个程序还没退干净）。"
              f"{e}\n请关闭旧的命令行窗口后重试，或改 config.json 里的 web_port。")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}"
    print("=" * 46)
    print("  观域 Voyant · AI 屏幕助手工作台")
    print(f"  控制台: {url}")
    print(f"  模型: {CONFIG['model']}（视觉+对话同一模型）")
    print("  关闭本窗口即停止程序")
    print("=" * 46)
    threading.Thread(target=monitor_loop, daemon=True, name="monitor").start()
    if CONFIG.get("open_browser", True) and not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
