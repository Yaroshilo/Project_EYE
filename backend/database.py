from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
# путь к файлу базы данных (создастся сам в папке backend)
DATABASE_URL = "sqlite:///./mkd_cctv.db"
# движок базы данных
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
# Фабрика сессий (каждый запрос клиента получает свою изолированную сессию работы с БД)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Базовый класс наследования таблиц
Base = declarative_base()
# Функция-генератор для FastAPI
# Открывает сессию при запросе и гарантированно закрывает её после ответа
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
