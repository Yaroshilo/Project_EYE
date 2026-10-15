import os
import sys
import time
import threading
import webbrowser
import uvicorn

def bootstrap():
    """Подготовка окружения и данных перед стартом"""
    os.makedirs("static/archive", exist_ok=True)
    
    # Инициализация схемы БД
    try:
        from database import Base, engine
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"[ERROR] DB init failed: {e}")
        sys.exit(1)

    # Проверка и генерация тестового видеоархива для аналитики
    try:
        from main import index_video_archive
        index_video_archive()
    except Exception as e:
        print(f"[WARN] Video index skipped: {e}")

def open_app():
    """Автоматическое открытие интерфейса в браузере"""
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    bootstrap()
    threading.Thread(target=open_app, daemon=True).start()
    print("\n[SERVER] мойКонсьерж запущен: http://127.0.0.1:8000\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")