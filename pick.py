"""屏幕框选工具：全屏半透明遮罩，拖出矩形后把区域打印到 stdout。

输出: {"region": [左, 上, 宽, 高]} 或 {}（按 Esc 取消）
由 bot.py 以子进程方式调用。
"""
import ctypes
import json
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    import tkinter as tk
except ImportError:
    print(json.dumps({"error": "本机 Python 缺少 tkinter"}))
    sys.exit(0)

root = tk.Tk()
root.attributes("-fullscreen", True)
try:
    root.attributes("-alpha", 0.25)
except Exception:
    pass
root.attributes("-topmost", True)
root.configure(bg="black")
root.title("框选区域")

cv = tk.Canvas(root, cursor="cross", bg="gray", highlightthickness=0)
cv.pack(fill="both", expand=True)

start = {"x": 0, "y": 0}
rect_id = None
done = {"flag": False}


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    done["flag"] = True
    root.destroy()


def on_press(e):
    start["x"], start["y"] = e.x, e.y
    global rect_id
    rect_id = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff2020", width=2)


def on_move(e):
    if rect_id is not None:
        cv.coords(rect_id, start["x"], start["y"], e.x, e.y)


def on_release(e):
    x1, y1 = start["x"], start["y"]
    x2, y2 = e.x, e.y
    region = [min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)]
    if region[2] < 30 or region[3] < 30:
        emit({})
    else:
        emit({"region": region})


def on_cancel(_):
    if not done["flag"]:
        emit({})


cv.bind("<ButtonPress-1>", on_press)
cv.bind("<B1-Motion>", on_move)
cv.bind("<ButtonRelease-1>", on_release)
root.bind("<Escape>", on_cancel)
root.mainloop()
