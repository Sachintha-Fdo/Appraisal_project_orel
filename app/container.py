from dataclasses import dataclass

from app.config import Settings, get_settings
from app.services.crypto_engine import AesGcmPiiVault, key_from_hex
from app.services.failed_queue import FailedRequestQueue
from app.services.pdf_service import BankStatementPdfService
from app.services.postgres_store import PostgresAuditStore
from app.services.presidio_masker import JsonPiiMasker
from app.services.redis_store import SessionStore
from app.services.relay_client import OutboundRelayClient


@dataclass
class AppContainer:
    settings: Settings
    store: SessionStore
    vault: AesGcmPiiVault
    masker: JsonPiiMasker
    pdf_service: BankStatementPdfService
    
    audit: PostgresAuditStore
    postgres: PostgresAuditStore
    
    relay: OutboundRelayClient
    failed_queue: FailedRequestQueue


def build_container(settings: Settings) -> AppContainer:
    vault = AesGcmPiiVault(key_from_hex(settings.pii_master_key_hex))
    store = SessionStore(settings)
    masker = JsonPiiMasker(settings, vault, store)
    pdf_service = BankStatementPdfService(settings)
    postgres = PostgresAuditStore(settings)
    relay = OutboundRelayClient(
        base_url=settings.cdb_local_server_url,
        settings=settings,
        outbound_api_key=settings.cdb_local_server_api_key,
    )
    failed_queue = FailedRequestQueue(settings)
    return AppContainer(
        settings=settings,
        store=store,
        vault=vault,
        masker=masker,
        pdf_service=pdf_service,
        audit=postgres,
        postgres=postgres,
        relay=relay,
        failed_queue=failed_queue,
    )


def validate_settings(settings: Settings) -> None:
    h = settings.pii_master_key_hex.strip().replace(" ", "")
    if len(h) != 64:
        raise RuntimeError(
            "PII_MASTER_KEY_HEX must be 64 hex characters (32 bytes) for AES-256-GCM"
        )
    bytes.fromhex(h)
