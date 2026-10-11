"""Mapped-source drag QA across real displays, without moving the cursor.

Uses synthetic data and isolated settings. Reads native window rectangles for
all steps. This is not a physical mouse test of the installed executable.
"""
import argparse
import ctypes
import json
import sys
import tempfile
import tkinter as tk
from ctypes import wintypes as W
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from data_client import normalize_provider


def work_areas():
    class Info(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]
    u = ctypes.windll.user32
    u.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.POINTER(Info)]
    callback = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
    result = []
    @callback
    def each(handle, _dc, _rect, _data):
        info = Info(size=ctypes.sizeof(Info))
        if not u.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        result.append((info.work.left, info.work.top, info.work.right, info.work.bottom))
        return True
    u.EnumDisplayMonitors(None, None, each, 0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    monitors = work_areas()
    if len(monitors) < 2:
        raise RuntimeError('Manual acceptance requires at least two connected monitors')
    u = ctypes.windll.user32
    u.GetAncestor.argtypes = [W.HWND, W.UINT]; u.GetAncestor.restype = W.HWND
    u.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
    samples = []
    with tempfile.TemporaryDirectory() as directory:
        settings = Path(directory) / 'settings.json'
        settings.write_text(json.dumps({'always_on_top': False, 'window_x': 80, 'window_y': 80}))
        with patch.object(app, 'SETTINGS_PATH', settings):
            root = tk.Tk()
            widget = app.UsageWidget(root, start_hidden=False, start_polling=False, create_tray=False)
            root.title('API Usage motion QA - synthetic data')
            widget.state['provider_quotas'] = {'providers': [normalize_provider('grok', {
                'limits': [{'used_percent': 25, 'label': 'Weekly'}]})]}
            widget.ui.render()

            def settle():
                root.update_idletasks(); root.update()

            def position():
                rect = W.RECT()
                assert u.GetWindowRect(u.GetAncestor(root.winfo_id(), 2), ctypes.byref(rect))
                return rect.left, rect.top

            def drag_to(x, y):
                target = widget.ui.subtitle
                anchor = target.winfo_rootx()+10, target.winfo_rooty()+10
                origin = position()
                target.event_generate('<ButtonPress-1>', x=10, y=10, rootx=anchor[0], rooty=anchor[1])
                try:
                    for step in range(1, 21):
                        expected = (round(origin[0]+(x-origin[0])*step/20), round(origin[1]+(y-origin[1])*step/20))
                        target.event_generate('<B1-Motion>', x=10, y=10,
                            rootx=anchor[0]+expected[0]-origin[0], rooty=anchor[1]+expected[1]-origin[1])
                        settle()
                        actual = position()
                        samples.append({'layout':widget.layout, 'expected':expected, 'actual':actual})
                        assert actual == expected, samples[-1]
                finally:
                    target.event_generate('<ButtonRelease-1>', x=10, y=10)
                    settle()
                assert position() == (x, y)
                saved = json.loads(settings.read_text())
                assert (saved['window_x'], saved['window_y']) == (x, y)

            try:
                settle()
                for layout in ('full', 'small'):
                    if widget.layout != layout:
                        widget.toggle_size(); settle()
                    for left, top, _right, _bottom in monitors:
                        drag_to(left+80, top+80)
                        expected = position()
                        widget.ui.render(); settle()
                        assert position() == expected
                        widget.hide(); widget.show(); settle()
                        assert position() == expected
                    drag_to(80, 80)
            finally:
                widget.quit()
    result = {'passed': True, 'source_mapped_window': True, 'physical_mouse_test': False,
              'monitor_count': len(monitors), 'moves_checked': len(samples), 'samples': samples}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'samples'}, indent=2))


if __name__ == '__main__':
    main()
