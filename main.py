import os
import runpy
from pathlib import Path

ENTRY_FILE = os.getenv("BOT_ENTRY", "guild_s9_2_hosting_ready.py").strip() or "guild_s9_2_hosting_ready.py"
entry_path = Path(ENTRY_FILE)

if not entry_path.exists():
    raise FileNotFoundError(
        f"Не найден файл запуска: {ENTRY_FILE}. "
        f"Положите основной файл бота в репозиторий или задайте BOT_ENTRY."
    )

runpy.run_path(str(entry_path), run_name="__main__")
