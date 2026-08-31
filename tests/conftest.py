from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db_session:
        yield db_session


@pytest.fixture
def html():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")
