"""Generate the README screenshot from synthetic data, never from an account."""
from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from data_client import normalize_provider


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    with tempfile.TemporaryDirectory() as temporary:
        settings = Path(temporary) / 'settings.json'
        settings.write_text(json.dumps({'always_on_top': False, 'window_x': 60, 'window_y': 60}))
        with patch.object(app, 'SETTINGS_PATH', settings), patch.object(app, '_system_uses_dark_mode', return_value=True):
            root = app.tk.Tk()
            widget = app.UsageWidget(root, start_hidden=False, start_polling=False, create_tray=False)
            providers = []
            for identifier, used, seconds in [('codex', 28, 14000), ('grok', 61, 180000)]:
                providers.append(normalize_provider(identifier, {'status': 'ok', 'source': 'codex-usage-api' if identifier == 'codex' else 'grok-cli-billing',
                    'limits': [{'id': identifier, 'group': identifier.title(), 'kind': 'account', 'label': 'Weekly',
                                'used_percent': used, 'reset_at': time.time()+seconds, 'window_seconds': 604800}]}))
            widget.state['provider_quotas'] = {'providers': providers}
            widget.ui.render()
            def capture():
                widget.ui.footer.configure(text='Preview · illustrative data, not a live account')
                root.update_idletasks()
                ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
                handle = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
                output = ROOT / 'docs' / 'usage-demo.png'
                output.parent.mkdir(exist_ok=True)
                ImageGrab.grab(window=handle).save(output)
                print(output)
                widget.quit()
            root.after(500, capture)
            root.mainloop()


if __name__ == '__main__':
    main()
