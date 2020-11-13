import tempfile
import os
import sys
import pytest

from unittest import mock
import responses
import mlflow


from mlflow.tracking.criteo_authentication import (
    register_criteo_authenticated_rest_store,
    _set_canonicalize_hostname_false,
    get_tracking_server_uri,
)
from mlflow.tracking._tracking_service.utils import (
    _tracking_store_registry,
    _TRACKING_TOKEN_ENV_VAR,
)


@responses.activate
@mock.patch("mlflow.tracking.criteo_authentication._generate_jwt_from_kerberos")
def test_authenticated_client_put_token_in_header(jtc_patch):
    responses.add(
        responses.GET,
        "https://mlflow.par.preprod.crto.in/api/2.0/mlflow/experiments/get?experiment_id=0",
        json={
            "experiment": {
                "experiment_id": "0",
                "name": "Default toto",
                "artifact_location": "hdfs://preprod-pa4/user/deepr/dev/mlflow_artifacts\r/0",
                "lifecycle_stage": "active",
            }
        },
        status=200,
    )
    jtc_patch.return_value = "my-token"
    old_store = _tracking_store_registry._registry["http"]
    register_criteo_authenticated_rest_store()
    mlflow.set_tracking_uri(get_tracking_server_uri())
    mlflow.get_experiment("0")
    assert responses.calls[0].request.headers["Authorization"] == "Bearer my-token"
    for scheme in ["http", "https"]:
        _tracking_store_registry.register(scheme, old_store)
    del os.environ[_TRACKING_TOKEN_ENV_VAR]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Command line only works on linux")
def test_set_canonicalize_hostname_false_on_existing_canonicalize_hostname():
    with tempfile.TemporaryDirectory() as work_dir:
        config_file = work_dir + "/krb5.conf"
        with open(config_file, "w") as f:
            f.write("[libdefaults]\ndns_canonicalize_hostname=true")
        _set_canonicalize_hostname_false(config_file)
        with open("/tmp/krb.hadoop.jtc.conf", "r") as f:
            krb5_conf = f.read()
            assert "dns_canonicalize_hostname = false" in krb5_conf
            assert "dns_canonicalize_hostname = true" not in krb5_conf
        os.remove("/tmp/krb.hadoop.jtc.conf")


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Command line only works on linux")
def test_set_canonicalize_hostname_false_on_non_existing_canonicalize_hostname():
    with tempfile.TemporaryDirectory() as work_dir:
        config_file = work_dir + "/krb5.conf"
        with open(config_file, "w") as f:
            f.write("[libdefaults]\ntoto=true")
        _set_canonicalize_hostname_false(config_file)
        with open("/tmp/krb.hadoop.jtc.conf", "r") as f:
            krb5_conf = f.read()
            assert "dns_canonicalize_hostname = false" in krb5_conf
        os.remove("/tmp/krb.hadoop.jtc.conf")
