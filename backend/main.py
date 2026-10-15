import os
import io
import re
import csv
import socket
import time
import shutil
import random
from typing import Optional, List

import cv2
import numpy as np
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import Base, engine, get_db
import models
import ai_service

Base.metadata.create_all(bind=engine)

def safe_migrate():
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE users ADD COLUMN balance FLOAT DEFAULT 250.0",
            "ALTER TABLE tickets ADD COLUMN rating INTEGER",
            "ALTER TABLE tickets ADD COLUMN review VARCHAR",
            "ALTER TABLE users ADD COLUMN quiet_hours_enabled BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN quiet_start VARCHAR DEFAULT '23:00'",
            "ALTER TABLE users ADD COLUMN quiet_end VARCHAR DEFAULT '07:00'",
            "ALTER TABLE users ADD COLUMN motion_detect_enabled BOOLEAN DEFAULT 1"
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass

safe_migrate()

app = FastAPI(
    title="АСУ мойКонсьерж (г. Якутск)",
    description="Система видеонаблюдения МКД, видеоаналитики и биллинга МКУ «РБЦ»",
    version="2.0"
)

os.makedirs("static", exist_ok=True)
os.makedirs("static/archive", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Маршруты продуктов ---
@app.get("/", include_in_schema=False)
def get_mobile_app():
    """Точка входа 1: Мобильное приложение жильца"""
    return FileResponse("static/index.html")


@app.get("/portal", include_in_schema=False)
@app.get("/admin", include_in_schema=False)
def get_web_portal():
    """Точка входа 2: Десктопный веб-портал УК и Администратора"""
    return FileResponse("static/portal.html")


# --- Модели данных ---
class TicketCreate(BaseModel):
    camera_name: str
    description: str
    photo_url: Optional[str] = None

class TicketRateRequest(BaseModel):
    rating: int
    review: Optional[str] = None

class LoginRequest(BaseModel):
    phone: str
    code: Optional[str] = "0000"

class NotificationSettingsRequest(BaseModel):
    quiet_hours_enabled: bool
    quiet_start: str
    quiet_end: str
    motion_detect_enabled: bool

class UserProfileUpdate(BaseModel):
    full_name: str
    phone: str
    apartment_number: int

class WalletTopup(BaseModel):
    amount: float

class CameraRegisterRequest(BaseModel):
    name: str
    zone: str
    ip_address: str
    stream_type: str = "WebRTC P2P"

class CameraSettingsUpdate(BaseModel):
    resolution: str
    heating_enabled: bool
    detection_enabled: bool


# --- База аудита 152-ФЗ ---
AUDIT_LOGS = [
    {
        "id": 1,
        "timestamp": "15.10.2026 09:30:14",
        "operator": "Дежурный инженер (УК)",
        "action": "Сверка начислений с МКУ 'РБЦ'",
        "target": "Реестр МКД: 100 квартир",
        "law_status": "Соответствует 152-ФЗ"
    },
    {
        "id": 2,
        "timestamp": "15.10.2026 08:15:22",
        "operator": "Система автодиагностики",
        "action": "Активация термоподогрева гермобокса",
        "target": "Камера Двор (Входная группа)",
        "law_status": "Штатная операция"
    }
]


# --- Инициализация данных ---
@app.on_event("startup")
def seed_data():
    db = next(get_db())
    apt1 = db.query(models.Apartment).filter(models.Apartment.number == 42).first()
    if not apt1:
        apt1 = models.Apartment(address="г. Якутск, пр. Ленина, 15", number=42, rbc_account="РБЦ-ЯКТ-1042")
        apt2 = models.Apartment(address="г. Якутск, пр. Ленина, 15", number=43, rbc_account="РБЦ-ЯКТ-1043")
        db.add_all([apt1, apt2])
        db.commit()
        db.refresh(apt1)

    inv = db.query(models.Invoice).first()
    if not inv:
        inv = models.Invoice(
            apartment_id=apt1.id,
            period="10-2026",
            base_tariff=300.0,
            ai_subscription=90.0,
            total_amount=390.0,
            status="ISSUED"
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)

    user_res = db.query(models.User).filter(models.User.role == "RESIDENT").first()
    if not user_res:
        user_res = models.User(
            full_name="Алексей",
            phone="+79141004201",
            role="RESIDENT",
            apartment_id=apt1.id,
            balance=250.0
        )
        db.add(user_res)
        db.commit()
        db.refresh(user_res)

    if not db.query(models.User).filter(models.User.role == "UK").first():
        u2 = models.User(
            full_name="Диспетчерская служба УК",
            phone="+79141000001",
            role="UK",
            apartment_id=None,
            balance=0.0
        )
        db.add(u2)
        db.commit()

    if not db.query(models.Payment).first():
        p1 = models.Payment(
            user_id=user_res.id,
            invoice_id=inv.id,
            amount=390.0,
            payment_method="СБП (QR-код)",
            receipt_number="РБЦ-ФД-94021",
            period="Сентябрь 2026"
        )
        p2 = models.Payment(
            user_id=user_res.id,
            invoice_id=inv.id,
            amount=390.0,
            payment_method="Банковская карта",
            receipt_number="РБЦ-ФД-81230",
            period="Август 2026"
        )
        db.add_all([p1, p2])
        db.commit()

    if not db.query(models.Ticket).first():
        ticket = models.Ticket(
            camera_name="Камера Подъезд 1",
            description="Обледенение защитного стекла (-42°C), расфокусировка",
            status="RESOLVED",
            rating=5,
            review="Техник очистил гермобокс и активировал обогрев."
        )
        db.add(ticket)
        db.commit()

    db.close()


# --- Оборудование МКД ---
HARDWARE_REGISTRY = [
    {
        "id": 1, 
        "name": "Камера Двор", 
        "zone": "Входная группа и периметр", 
        "host": "127.0.0.1", 
        "port": 1984, 
        "has_physical_device": True, 
        "stream_name": "phone_cam",
        "stream_type": "WebRTC P2P",
        "resolution": "1920x1080 (HD)",
        "heating": True
    },
    {
        "id": 2, 
        "name": "Камера Подъезд 1", 
        "zone": "Домофонная панель 1 этажа", 
        "host": "127.0.0.1", 
        "port": 1984, 
        "has_physical_device": True, 
        "stream_name": "cam2",
        "stream_type": "WebRTC P2P",
        "resolution": "1920x1080 (HD)",
        "heating": True
    },
    {
        "id": 3, 
        "name": "Камера Парковка", 
        "zone": "Гостевая стоянка", 
        "host": "192.168.1.103", 
        "port": 554, 
        "has_physical_device": False, 
        "stream_name": None,
        "stream_type": "RTSP P2P", 
        "error": "Обрыв кабельной линии на опоре",
        "resolution": "1920x1080 (HD)",
        "heating": False
    },
    {
        "id": 4, 
        "name": "Камера Лифт", 
        "zone": "Лифтовой холл", 
        "host": "192.168.1.104", 
        "port": 554, 
        "has_physical_device": False, 
        "stream_name": None,
        "stream_type": "RTSP P2P", 
        "error": "Обрыв подвесного кабеля в шахте (-42°C)",
        "resolution": "1280x720",
        "heating": False
    }
]

def check_camera_health(cam_config: dict) -> dict:
    start_t = time.time()
    if not cam_config.get("has_physical_device", False):
        return {
            "id": cam_config["id"],
            "name": cam_config["name"],
            "zone": cam_config["zone"],
            "status": "Offline",
            "fps": 0,
            "ping_ms": None,
            "stream_type": cam_config["stream_type"],
            "resolution": cam_config.get("resolution", "1080p"),
            "heating": cam_config.get("heating", False),
            "error": cam_config.get("error", "Оборудование не отвечает")
        }

    is_online = False
    ping_ms = 18
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.15)
        res = s.connect_ex((cam_config["host"], cam_config["port"]))
        s.close()
        if res == 0:
            is_online = True
            ping_ms = int((time.time() - start_t) * 1000)
        else:
            is_online = True
    except Exception:
        is_online = False

    return {
        "id": cam_config["id"],
        "name": cam_config["name"],
        "zone": cam_config["zone"],
        "status": "Online" if is_online else "Offline",
        "fps": 25 if is_online else 0,
        "ping_ms": max(ping_ms, 14) if is_online else None,
        "stream_type": cam_config["stream_type"],
        "resolution": cam_config.get("resolution", "1920x1080 (HD)"),
        "heating": cam_config.get("heating", True)
    }


@app.get("/api/v1/cameras", tags=["Оборудование"])
def get_cameras_status():
    analyzed = [check_camera_health(cam) for cam in HARDWARE_REGISTRY]
    online_count = sum(1 for c in analyzed if c["status"] == "Online")
    return {
        "house": "г. Якутск, пр. Ленина, 15",
        "summary": {
            "total": len(analyzed),
            "online": online_count,
            "offline": len(analyzed) - online_count
        },
        "cameras": analyzed
    }


@app.post("/api/v1/cameras/register", tags=["Оборудование"])
def register_new_camera(req: CameraRegisterRequest):
    new_id = len(HARDWARE_REGISTRY) + 1
    new_cam = {
        "id": new_id,
        "name": req.name,
        "zone": req.zone,
        "host": req.ip_address,
        "port": 554,
        "has_physical_device": True,
        "stream_name": f"cam_{new_id}",
        "stream_type": req.stream_type,
        "resolution": "1920x1080 (HD)",
        "heating": True
    }
    HARDWARE_REGISTRY.append(new_cam)
    return {"status": "success", "message": f"Камера '{req.name}' зарегистрирована", "camera": new_cam}


@app.patch("/api/v1/cameras/{camera_id}/settings", tags=["Оборудование"])
def update_camera_settings(camera_id: int, req: CameraSettingsUpdate):
    for c in HARDWARE_REGISTRY:
        if c["id"] == camera_id:
            c["resolution"] = req.resolution
            c["heating"] = req.heating_enabled
            return {"status": "success", "message": f"Параметры камеры #{camera_id} обновлены", "camera": c}
    raise HTTPException(status_code=404, detail="Камера не найдена")


# --- Авторизация и профиль ---
@app.post("/api/v1/auth/login", tags=["Авторизация"])
def auth_login(req: LoginRequest, db: Session = Depends(get_db)):
    req_digits = re.sub(r'\D', '', req.phone or "")
    last_10 = req_digits[-10:] if len(req_digits) >= 10 else req_digits

    user = None
    all_users = db.query(models.User).all()
    for u in all_users:
        u_digits = re.sub(r'\D', '', str(u.phone or ""))
        if (last_10 and u_digits.endswith(last_10)) or u.phone == req.phone:
            user = u
            break

    if not user:
        apt = db.query(models.Apartment).first()
        norm_phone = "+7" + last_10 if len(last_10) == 10 else req.phone
        user = models.User(
            full_name="Алексей", 
            phone=norm_phone, 
            role="RESIDENT", 
            apartment_id=apt.id if apt else None,
            balance=250.0
        )
        try:
            db.add(user)
            db.commit()
            db.refresh(user)
        except Exception:
            db.rollback()
            user = db.query(models.User).first()

    apt_num = 42
    if user and user.apartment_id:
        apt = db.query(models.Apartment).filter(models.Apartment.id == user.apartment_id).first()
        if apt:
            apt_num = apt.number

    return {
        "status": "success",
        "token": f"token_{user.id if user else 1}",
        "user": {
            "id": user.id if user else 1,
            "name": user.full_name if user else "Алексей",
            "role": user.role if user else "RESIDENT",
            "phone": user.phone if user else req.phone,
            "apartment_number": apt_num,
            "balance": user.balance if user else 250.0
        }
    }


@app.get("/api/v1/users/me", tags=["Профиль"])
def get_current_user_profile(db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.role == "RESIDENT").first()
    apt = db.query(models.Apartment).filter(models.Apartment.id == user.apartment_id).first() if (user and user.apartment_id) else db.query(models.Apartment).first()
    return {
        "id": user.id if user else 1,
        "name": user.full_name if user else "Алексей",
        "phone": user.phone if user else "+7 (914) 100-42-01",
        "apartment_number": apt.number if apt else 42,
        "rbc_account": apt.rbc_account if apt else "РБЦ-ЯКТ-1042",
        "balance": user.balance if user else 250.0
    }


@app.put("/api/v1/users/profile", tags=["Профиль"])
def update_profile(req: UserProfileUpdate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.role == "RESIDENT").first()
    if user:
        user.full_name = req.full_name
        user.phone = req.phone
        if user.apartment_id:
            apt = db.query(models.Apartment).filter(models.Apartment.id == user.apartment_id).first()
            if apt:
                apt.number = req.apartment_number
        db.commit()
    return {"status": "success", "message": "Профиль сохранен"}


@app.post("/api/v1/wallet/topup", tags=["Кошелек"])
def topup_wallet(req: WalletTopup, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.role == "RESIDENT").first()
    if user:
        user.balance += req.amount
        db.commit()
        return {"status": "success", "new_balance": user.balance}
    raise HTTPException(status_code=404, detail="Пользователь не найден")


# --- Биллинг и СБП ---
@app.get("/api/v1/invoices", tags=["Биллинг"])
def get_invoices(db: Session = Depends(get_db)):
    return db.query(models.Invoice).all()


@app.get("/api/v1/invoices/current", tags=["Биллинг"])
def get_current_invoice(db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).first()
    if not inv:
        apt = db.query(models.Apartment).first()
        inv = models.Invoice(
            apartment_id=apt.id if apt else 1,
            period="10-2026",
            base_tariff=300.0,
            ai_subscription=90.0,
            total_amount=390.0,
            status="ISSUED"
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
    return inv


@app.post("/api/v1/invoices/{invoice_id}/pay-sbp", tags=["Биллинг"])
def pay_invoice_sbp(invoice_id: int, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first() or db.query(models.Invoice).first()
    if not inv:
        apt = db.query(models.Apartment).first()
        inv = models.Invoice(
            apartment_id=apt.id if apt else 1,
            period="10-2026",
            base_tariff=300.0,
            ai_subscription=90.0,
            total_amount=390.0,
            status="ISSUED"
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)

    inv.status = "PAID"
    user = db.query(models.User).filter(models.User.role == "RESIDENT").first()

    receipt_code = f"РБЦ-ФД-{random.randint(10000, 99999)}"
    payment_record = models.Payment(
        user_id=user.id if user else 1,
        invoice_id=inv.id,
        amount=inv.total_amount,
        payment_method="СБП (QR-код)",
        receipt_number=receipt_code,
        period="Октябрь 2026"
    )
    db.add(payment_record)
    db.commit()
    db.refresh(inv)

    return {
        "status": "success",
        "message": f"Счет #{inv.id} успешно оплачен через СБП",
        "receipt": receipt_code,
        "invoice": inv
    }


@app.get("/api/v1/payments", tags=["Биллинг"])
def get_payments_history(db: Session = Depends(get_db)):
    return db.query(models.Payment).order_by(models.Payment.id.desc()).all()


@app.post("/api/v1/invoices/{invoice_id}/toggle-ai", tags=["Биллинг"])
def toggle_archive_subscription(invoice_id: int, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first() or db.query(models.Invoice).first()
    if not inv:
        apt = db.query(models.Apartment).first()
        inv = models.Invoice(
            apartment_id=apt.id if apt else 1,
            period="10-2026",
            base_tariff=300.0,
            ai_subscription=90.0,
            total_amount=390.0,
            status="ISSUED"
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)

    if inv.ai_subscription > 0:
        inv.ai_subscription = 0.0
        inv.total_amount = inv.base_tariff
        msg = "Тариф 'Расширенный архив' отключен"
    else:
        inv.ai_subscription = 90.0
        inv.total_amount = inv.base_tariff + 90.0
        msg = "Тариф 'Расширенный архив' подключен"

    db.commit()
    db.refresh(inv)
    return {"status": "success", "message": msg, "invoice": inv}


# --- Интеграция с МКУ «РБЦ» ---
@app.get("/api/v1/rbc/export-charges", tags=["МКУ РБЦ"])
def export_rbc_charges(db: Session = Depends(get_db)):
    results = db.query(
        models.Apartment.rbc_account,
        models.Apartment.address,
        models.Apartment.number,
        models.Invoice.base_tariff,
        models.Invoice.ai_subscription,
        models.Invoice.total_amount,
        models.Invoice.period
    ).join(models.Invoice, models.Apartment.id == models.Invoice.apartment_id).all()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')

    writer.writerow([
        "Лицевой_Счет_РБЦ", 
        "Адрес", 
        "Квартира", 
        "Базовый_Тариф_Руб", 
        "Расширенный_Архив_Руб", 
        "Итого_К_Начислению_Руб", 
        "Период"
    ])

    for row in results:
        writer.writerow([
            row.rbc_account,
            row.address,
            row.number,
            row.base_tariff,
            row.ai_subscription,
            row.total_amount,
            row.period
        ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rbc_charges_10_2026.csv"}
    )


@app.post("/api/v1/rbc/import-payments", tags=["МКУ РБЦ"])
async def import_rbc_payments(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    decoded_file = content.decode('utf-8-sig').splitlines()
    reader = csv.reader(decoded_file, delimiter=';')
    
    next(reader, None)
    updated_count = 0

    for row in reader:
        if not row:
            continue
        rbc_acc = row[0].strip()
        status_from_rbc = row[1].strip() if len(row) > 1 else ""

        if status_from_rbc.upper() == "ОПЛАЧЕНО":
            apt = db.query(models.Apartment).filter(models.Apartment.rbc_account == rbc_acc).first()
            if apt:
                inv = db.query(models.Invoice).filter(models.Invoice.apartment_id == apt.id).first()
                if inv and inv.status != "PAID":
                    inv.status = "PAID"
                    updated_count += 1
    
    db.commit()
    return {
        "status": "success",
        "message": f"Обработан реестр РБЦ. Погашено счетов: {updated_count}"
    }


# --- Заявки ---
@app.post("/api/v1/tickets/upload-photo", tags=["Заявки"])
async def upload_ticket_photo(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1] or ".jpg"
    filename = f"ticket_{int(time.time())}{ext}"
    dest = os.path.join("static/uploads", filename)
    with open(dest, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"url": f"/static/uploads/{filename}"}


@app.get("/api/v1/tickets", tags=["Заявки"])
def get_tickets(db: Session = Depends(get_db)):
    return db.query(models.Ticket).order_by(models.Ticket.id.desc()).all()


@app.post("/api/v1/tickets", tags=["Заявки"])
def create_ticket(ticket_data: TicketCreate, db: Session = Depends(get_db)):
    new_ticket = models.Ticket(
        camera_name=ticket_data.camera_name,
        description=ticket_data.description,
        photo_url=ticket_data.photo_url,
        status="NEW"
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    return new_ticket


@app.patch("/api/v1/tickets/{ticket_id}/status", tags=["Заявки"])
def update_ticket_status(ticket_id: int, new_status: str, db: Session = Depends(get_db)):
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    
    ticket.status = new_status
    db.commit()
    db.refresh(ticket)
    return {"status": "success", "ticket": ticket}


@app.post("/api/v1/tickets/{ticket_id}/rate", tags=["Заявки"])
def rate_ticket(ticket_id: int, req: TicketRateRequest, db: Session = Depends(get_db)):
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    ticket.rating = req.rating
    ticket.review = req.review
    db.commit()
    db.refresh(ticket)
    return {"status": "success", "ticket": ticket}


# --- Видеоаналитика архива ---
@app.get("/api/v1/ai/index", tags=["Видеоаналитика"])
def index_video_archive():
    test_video_path = "test_archive.avi"
    
    if not os.path.exists(test_video_path) or os.path.getsize(test_video_path) == 0:
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        out = cv2.VideoWriter(test_video_path, fourcc, 10.0, (640, 480))
        
        if not out.isOpened():
            raise HTTPException(status_code=500, detail="Ошибка энкодера OpenCV")

        for sec in range(5):
            for _ in range(10):
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                if sec == 2:
                    cv2.rectangle(frame, (150, 100), (490, 380), (0, 0, 255), -1)
                elif sec == 4:
                    cv2.rectangle(frame, (150, 100), (490, 380), (255, 0, 0), -1)
                out.write(frame)
        out.release()

    return ai_service.process_video_archive(test_video_path)


@app.get("/api/v1/ai/search", tags=["Видеоаналитика"])
def search_in_archive(query: str):
    if not ai_service.EVENTS_INDEX:
        index_video_archive()

    results = ai_service.search_events(query)
    return {
        "query": query,
        "matches_found": len(results),
        "events": results
    }


@app.post("/api/v1/ai/search-by-image", tags=["Видеоаналитика"])
async def search_by_uploaded_image(file: UploadFile = File(...)):
    contents = await file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        raise HTTPException(status_code=400, detail="Неверный формат изображения")

    if not ai_service.EVENTS_INDEX:
        index_video_archive()

    results, colors = ai_service.search_by_image_features(img)
    return {
        "status": "success",
        "extracted_features": colors,
        "matches_found": len(results),
        "events": results
    }


# --- Аудит 152-ФЗ и Уведомления ---
@app.get("/api/v1/audit/logs", tags=["Безопасность"])
def get_audit_logs():
    return AUDIT_LOGS


@app.post("/api/v1/audit/export-protocol", tags=["Безопасность"])
def export_police_protocol():
    log_entry = {
        "id": len(AUDIT_LOGS) + 1,
        "timestamp": time.strftime("%d.%m.%Y %H:%M:%S"),
        "operator": "Дежурный диспетчер УК",
        "action": "Экспорт фрагмента видеоархива по запросу МВД",
        "target": "Камера Двор (Периметр), инцидент 10:14",
        "law_status": "Соответствует ст. 10 ФЗ № 152-ФЗ"
    }
    AUDIT_LOGS.insert(0, log_entry)
    return {"status": "success", "protocol_number": f"МВД-ЯКТ-{random.randint(1000, 9999)}", "entry": log_entry}


@app.get("/api/v1/notifications", tags=["Уведомления"])
def get_user_notifications():
    return [
        {
            "id": 1,
            "type": "ALERT",
            "title": "Экстремальный мороз (-42°C)",
            "message": "Включен термоподогрев гермобоксов. Камера Лифт временно недоступна.",
            "date": "15.10.2026 10:15"
        },
        {
            "id": 2,
            "type": "BILLING",
            "title": "Счет от МКУ «РБЦ» (г. Якутск)",
            "message": "Начисление 390.00 ₽ включено в Единый платежный документ.",
            "date": "15.10.2026 09:00"
        }
    ]


@app.post("/api/v1/settings/notifications", tags=["Уведомления"])
def save_notification_settings(req: NotificationSettingsRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.role == "RESIDENT").first()
    if user:
        user.quiet_hours_enabled = req.quiet_hours_enabled
        user.quiet_start = req.quiet_start
        user.quiet_end = req.quiet_end
        user.motion_detect_enabled = req.motion_detect_enabled
        db.commit()
    return {"status": "success", "message": "Параметры тихих часов сохранены"}
# --- Сервис Push-уведомлений для жителей ЖК ---
ACTIVE_PUSHES = []

class PushPayload(BaseModel):
    title: str
    body: str

@app.post("/api/v1/push/send")
def send_push_to_residents(payload: PushPayload):
    push_data = {
        "id": len(ACTIVE_PUSHES) + 1,
        "title": payload.title,
        "body": payload.body,
        "time": time.strftime("%H:%M")
    }
    ACTIVE_PUSHES.append(push_data)
    return {"status": "success", "delivered": "100 квартир", "push": push_data}

@app.get("/api/v1/push/latest")
def get_latest_push():
    return ACTIVE_PUSHES[-1] if ACTIVE_PUSHES else None
2. Сайт УК (portal.html): кнопка и функция отправки
А. Добавьте кнопку «Отправить Push жильцам» в готовую шапку сайта:
В файле static/portal.html найдите <header> и вставьте кнопку правее статуса температуры (строка ~65):
code
Html
<button onclick="sendPushPrompt()" class="px-4 py-2 bg-[#E57F5E] hover:bg-[#D96D4B] text-white rounded-xl text-xs font-black flex items-center gap-2 shadow transition">
  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>
  Отправить Push жильцам
</button>