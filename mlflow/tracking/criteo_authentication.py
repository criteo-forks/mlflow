import os
import sys
from typing import Any
import subprocess

import requests

from mlflow.store.tracking.rest_store import RestStore
from mlflow.utils.rest_utils import MlflowHostCreds
from mlflow.tracking._tracking_service.utils import (
    _tracking_store_registry,
    _TRACKING_TOKEN_ENV_VAR,
)


def get_tracking_server_uri() -> str:
    env = os.getenv("CRITEO_ENV", "preprod").lower()
    domain = "prod" if env == "prod" else "preprod"
    return "https://mlflow.par." + domain + ".crto.in"


# pylint: disable=unused-argument
def _get_authenticated_rest_store(store_uri: str, **_: Any) -> RestStore:
    def _return_token(force_refresh_token: bool = False) -> MlflowHostCreds:
        if force_refresh_token:
            token = _generate_jwt_from_kerberos().replace("Bearer ", "")
            os.environ[_TRACKING_TOKEN_ENV_VAR] = token
        return MlflowHostCreds(
            host=get_tracking_server_uri(), token=os.getenv(_TRACKING_TOKEN_ENV_VAR, "")
        )

    return RestStore(_return_token)


def _generate_jwt_from_kerberos():
    from requests_gssapi import HTTPSPNEGOAuth  # pylint: disable=import-error

    _set_canonicalize_hostname_false()
    auth = HTTPSPNEGOAuth()
    if os.getenv("CRITEO_ENV", "dev").lower() == "prod":
        jtc_url = "https://jtc.prod.crto.in/spnego/generate/jwt"
    else:
        jtc_url = "https://jtc.preprod.crto.in/spnego/generate/jwt"
    jtc_request = requests.get(jtc_url, auth=auth)
    if jtc_request.status_code != 200:
        raise Exception("Failed to get a token from the jtc. " + jtc_request.text)
    return jtc_request.json()["jwt"]


def _set_canonicalize_hostname_false(config_file: str = "/etc/krb5.conf") -> None:
    if sys.platform != "win32":
        cmd = (
            "grep -vE '^.*dns_canonicalize_hostname.*=.*' "
            + config_file
            + " | sed 's/\\[libdefaults\\]/\\[libdefaults\\]\\n  dns_canonicalize_hostname = false/"
            "' > /tmp/krb.hadoop.jtc.conf"
        )
        subprocess.check_output(cmd, shell=True)
        os.environ["KRB5_CONFIG"] = "/tmp/krb.hadoop.jtc.conf"


def register_criteo_authenticated_rest_store() -> None:
    for scheme in ["http", "https"]:
        _tracking_store_registry.register(scheme, _get_authenticated_rest_store)
