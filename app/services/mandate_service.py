import uuid

from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.mandate import MandateCreate


def create_mandate(db: Session, data: MandateCreate) -> Mandate:
    user = db.get(User, data.user_id)
    if user is None:
        raise NotFoundError(f"User {data.user_id} not found.")

    mandate = Mandate(
        id=uuid.uuid4(),
        user_id=data.user_id,
        name=data.name,
        purpose=data.purpose,
        currency=data.currency,
        total_authority=data.total_authority,
        status=MandateStatus.ACTIVE,
        not_before=data.not_before,
        not_after=data.not_after,
    )
    db.add(mandate)
    db.flush()
    return mandate


def get_mandate(db: Session, mandate_id: uuid.UUID) -> Mandate:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise NotFoundError(f"Mandate {mandate_id} not found.")
    return mandate
