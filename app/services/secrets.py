from __future__ import annotations

from abc import ABC, abstractmethod

from cryptography.fernet import Fernet


class SecretStorage(ABC):
    @abstractmethod
    def encrypt(self, value: str) -> str: ...

    @abstractmethod
    def decrypt(self, value: str) -> str: ...


class FernetSecretStorage(SecretStorage):
    def __init__(self, key: str):
        if not key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY обязателен для хранения OAuth-токена")
        self.fernet = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
