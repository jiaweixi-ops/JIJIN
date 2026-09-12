from __future__ import annotations

import pytest

from app.config import Settings


def test_prod_requires_independent_service_token_and_credential_pepper():
    with pytest.raises(RuntimeError, match="INTERNAL_API_TOKEN"):
        Settings(app_env="prod", internal_api_token="", api_credential_pepper="pepper").validate_runtime()

    with pytest.raises(RuntimeError, match="API_CREDENTIAL_PEPPER"):
        Settings(app_env="prod", internal_api_token="service", api_credential_pepper="").validate_runtime()

    with pytest.raises(RuntimeError, match="must be different"):
        Settings(
            app_env="prod",
            internal_api_token="same-secret",
            api_credential_pepper="same-secret",
        ).validate_runtime()

    Settings(
        app_env="prod",
        internal_api_token="service-token-A",
        api_credential_pepper="credential-pepper-B",
    ).validate_runtime()
