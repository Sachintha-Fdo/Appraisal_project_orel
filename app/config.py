from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.services.settings_utils import parse_comma_list


class Settings(BaseSettings):
    
    model_config = SettingsConfigDict(
        env_file=(".env", "endpoints.env"), env_file_encoding="utf-8", extra="ignore"
    )

    redis_url: str = "redis://127.0.0.1:6379/0"
    use_fake_redis: bool = False
    pii_master_key_hex: str = ""
    pii_redis_ttl_seconds: int = 60 * 60 * 24 * 7

   
    cdb_local_server_url: str = ""
    
    cdb_local_server_api_key: str = ""
    azure_callback_token: str = ""
    
    echo_azure: bool = False
    
    azure_request_timeout_seconds: float = 300.0
    
    azure_max_attempts: int = 2
    azure_retry_backoff_seconds: float = 2.0
    
    azure_retry_status_codes: Annotated[list[int], NoDecode] = [502, 503, 504]

    
    failed_queue_key: str = "failed_requests"
    failed_queue_max_len: int = 5000

    sample_images_path: str = "sample_images"
    uploads_dir: str = "uploads"
    
    masked_pdfs_dir: str = "masked_pdfs"
    
    vendor_exchange_dir: str = "vendor_exchanges"

    database_url: str = ""
    
    audit_retention_days: int = 30

    presidio_language: str = "en"
    presidio_spacy_model: str = "en_core_web_sm"
    json_mask_keys: Annotated[list[str], NoDecode] = []
    public_base_url: str = "http://127.0.0.1:8000"

    
    api_keys: Annotated[list[str], NoDecode] = []
    log_level: str = "INFO"

    # PDF OCR masking (PaddleOCR) 
    pdf_ocr_zoom: int = 4
    
    pdf_mask_timeout_seconds: float = 600.0

    @field_validator("json_mask_keys", mode="before")
    @classmethod
    def _parse_json_mask_keys(cls, value: Any) -> list[str]:
        return parse_comma_list(value, transform=lambda s: s.lower(), default=[])

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_api_keys(cls, value: Any) -> list[str]:
        return parse_comma_list(value, transform=str, default=[])

    @field_validator("azure_retry_status_codes", mode="before")
    @classmethod
    def _parse_azure_retry_status_codes(cls, value: Any) -> list[int]:
        return parse_comma_list(value, transform=int, default=[502, 503, 504])


@lru_cache
def get_settings() -> Settings:
    return Settings()
