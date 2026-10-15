from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.sql import func
from database import Base

class Apartment(Base):
    __tablename__ = "apartments"

    id = Column(Integer, primary_key=True, index=True)
    address = Column(String, default="г. Якутск, пр. Ленина, 15")
    number = Column(Integer, default=42)
    rbc_account = Column(String, unique=True, index=True, default="РБЦ-ЯКТ-1042")

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, default="Собственник (кв. 42)")
    phone = Column(String, unique=True, index=True, default="+79141004201")
    role = Column(String, default="RESIDENT")
    apartment_id = Column(Integer, ForeignKey("apartments.id"), nullable=True)
    
    # Личный кошелек / баланс
    balance = Column(Float, default=150.0)

    # Настройки уведомлений (FR-2, FR-8)
    quiet_hours_enabled = Column(Boolean, default=True)
    quiet_start = Column(String, default="23:00")
    quiet_end = Column(String, default="07:00")
    motion_detect_enabled = Column(Boolean, default=True)

class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    apartment_id = Column(Integer, ForeignKey("apartments.id"))
    period = Column(String, default="10-2026")  
    base_tariff = Column(Float, default=300.0)
    ai_subscription = Column(Float, default=90.0)
    total_amount = Column(Float, default=390.0)
    status = Column(String, default="ISSUED")  # ISSUED (Не оплачен), PAID (Оплачен)

# Реальная таблица транзакций и чеков (FR-3, FR-11)
class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    amount = Column(Float)
    payment_method = Column(String, default="СБП (НСПК)") # СБП, Баланс кошелька, Карта
    receipt_number = Column(String)
    period = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    camera_name = Column(String) 
    description = Column(String) 
    photo_url = Column(String, nullable=True)
    status = Column(String, default="NEW")
    rating = Column(Integer, nullable=True)
    review = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())