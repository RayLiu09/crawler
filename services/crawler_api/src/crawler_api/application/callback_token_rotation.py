import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from crawler_api.domain.callbacks import CollectionJobCallbackCredentialRotation
from crawler_api.domain.enums import TERMINAL_COLLECTION_STATES
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_contracts.collection_jobs import RotateCollectionJobCallbackTokenRequest
from crawler_contracts.common import ApiScope
from pydantic import SecretStr

from .caller_authentication import CallerAccessTokenAuthenticator
from .collection_jobs import (
    CALLBACK_TOKEN_TTL,
    InvalidIdempotencyKey,
    hash_idempotency_key,
)
from .ports import UnitOfWork


class CallbackTokenRotationNotFound(Exception):
    pass


class CallbackTokenRotationConflict(Exception):
    pass


class CallbackTokenRotationInvalid(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CallbackTokenRotationResult:
    callback_token_expires_at: datetime
    credential_version: int


def rotation_request_fingerprint(
    token_fingerprint: str,
    token_expires_at: datetime,
) -> str:
    canonical = json.dumps(
        {
            "tokenExpiresAt": token_expires_at.astimezone(UTC).isoformat(),
            "tokenFingerprint": token_fingerprint,
            "tokenType": "Bearer",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class CollectionJobCallbackTokenRotator:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        callback_token_protector: CallbackTokenProtector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._callback_token_protector = callback_token_protector
        self._clock = clock

    async def rotate(
        self,
        *,
        access_token: SecretStr,
        idempotency_key: SecretStr,
        job_id: UUID,
        request: RotateCollectionJobCallbackTokenRequest,
    ) -> CallbackTokenRotationResult:
        raw_idempotency_key = idempotency_key.get_secret_value()
        if not raw_idempotency_key or len(raw_idempotency_key) > 200:
            raise InvalidIdempotencyKey

        rotated_at = self._clock()
        key_hash = hash_idempotency_key(idempotency_key)
        callback_fingerprint = self._callback_token_protector.fingerprint(request.access_token)

        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_CONTROL,
                authenticated_at=rotated_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client_for_update(
                job_id, caller.client.id
            )
            if job is None:
                raise CallbackTokenRotationNotFound
            stored = await unit_of_work.callback_configs.get_for_job(job.id)
            if stored is None:
                raise CallbackTokenRotationNotFound

            replay = await unit_of_work.callback_credential_rotations.get(stored.id, key_hash)
            if replay is not None:
                effective_expiry = request.expires_at or replay.token_expires_at
                request_fingerprint = rotation_request_fingerprint(
                    callback_fingerprint, effective_expiry
                )
                if not hmac.compare_digest(replay.request_fingerprint, request_fingerprint):
                    raise CallbackTokenRotationConflict
                result = CallbackTokenRotationResult(
                    callback_token_expires_at=replay.token_expires_at,
                    credential_version=replay.result_credential_version,
                )
                await unit_of_work.commit()
                return result

            if stored.credential_destroyed_at is not None:
                raise CallbackTokenRotationConflict

            token_expires_at = request.expires_at or (rotated_at + CALLBACK_TOKEN_TTL)
            self._validate_new_credential(
                caller_access_token=access_token,
                callback_access_token=request.access_token,
                token_expires_at=token_expires_at,
                rotated_at=rotated_at,
            )
            if (
                job.status in TERMINAL_COLLECTION_STATES
                and not await unit_of_work.webhook_outbox.has_undelivered_for_callback(stored.id)
            ):
                raise CallbackTokenRotationConflict

            previous_version = stored.credential_version
            next_version = previous_version + 1
            protected = self._callback_token_protector.protect(
                request.access_token,
                callback_config_id=stored.id,
                collection_job_id=job.id,
                credential_version=next_version,
            )
            updated = replace(
                stored,
                token_ciphertext=protected.ciphertext,
                token_nonce=protected.nonce,
                token_fingerprint=protected.fingerprint,
                token_expires_at=token_expires_at,
                credential_version=next_version,
                encryption_key_version=protected.encryption_key_version,
                updated_at=rotated_at,
            )
            if not await unit_of_work.callback_configs.update_credentials(
                updated,
                expected_credential_version=previous_version,
                updated_at=rotated_at,
            ):
                raise CallbackTokenRotationConflict

            await unit_of_work.callback_credential_rotations.add(
                CollectionJobCallbackCredentialRotation(
                    callback_config_id=stored.id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=rotation_request_fingerprint(
                        protected.fingerprint, token_expires_at
                    ),
                    result_credential_version=next_version,
                    token_expires_at=token_expires_at,
                    created_at=rotated_at,
                )
            )
            await unit_of_work.webhook_outbox.resume_credential_blocked(stored.id, rotated_at)
            await unit_of_work.commit()
            return CallbackTokenRotationResult(
                callback_token_expires_at=token_expires_at,
                credential_version=next_version,
            )

    @staticmethod
    def _validate_new_credential(
        *,
        caller_access_token: SecretStr,
        callback_access_token: SecretStr,
        token_expires_at: datetime,
        rotated_at: datetime,
    ) -> None:
        if hmac.compare_digest(
            caller_access_token.get_secret_value(),
            callback_access_token.get_secret_value(),
        ):
            raise CallbackTokenRotationInvalid
        if token_expires_at <= rotated_at or token_expires_at > rotated_at + CALLBACK_TOKEN_TTL:
            raise CallbackTokenRotationInvalid
