import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.capability import MandateLedger
from app.schemas.mandate import MandateCreate, MandateRead
from app.services import capability_service, mandate_service

router = APIRouter(prefix="/mandates", tags=["mandates"])


@router.post("", response_model=MandateRead, status_code=201)
def create_mandate(payload: MandateCreate, db: Session = Depends(get_db)):
    mandate = mandate_service.create_mandate(db, payload)
    db.commit()
    db.refresh(mandate)
    return mandate


@router.get("/{mandate_id}", response_model=MandateRead)
def get_mandate(mandate_id: uuid.UUID, db: Session = Depends(get_db)):
    return mandate_service.get_mandate(db, mandate_id)


@router.get("/{mandate_id}/ledger", response_model=MandateLedger)
def get_ledger(mandate_id: uuid.UUID, db: Session = Depends(get_db)):
    mandate_service.get_mandate(db, mandate_id)  # raises 404 if missing
    return capability_service.compute_ledger(db, mandate_id)
