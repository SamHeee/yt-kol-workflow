import logging

import pytest

from feishu.bitable import BitableClient


PRIVATE_EMAIL = "private.person@example.com"
PRIVATE_ADDRESS = "88 Sensitive Street"
PRIVATE_TOKEN = "tenant_access_token=super-secret-token"


class FailingClient:
    def _post(self, path, data):
        return {
            "code": 1254064,
            "msg": (
                f"invalid value {PRIVATE_EMAIL} at {PRIVATE_ADDRESS}; "
                f"{PRIVATE_TOKEN}; payload={data}"
            ),
        }


@pytest.mark.parametrize(
    "method_name, records",
    [
        (
            "batch_create_records",
            [{"fields": {"Email": PRIVATE_EMAIL, "Address": PRIVATE_ADDRESS}}],
        ),
        (
            "batch_create_records_with_ids",
            [{"fields": {"Email": PRIVATE_EMAIL, "Address": PRIVATE_ADDRESS}}],
        ),
        (
            "batch_update_records",
            [{
                "record_id": "rec-private-identifier",
                "fields": {"Email": PRIVATE_EMAIL, "Address": PRIVATE_ADDRESS},
            }],
        ),
    ],
)
def test_batch_failures_log_only_safe_structural_diagnostics(
    caplog, method_name, records
):
    caplog.set_level(logging.DEBUG, logger="kol_workflow.feishu.bitable")

    with pytest.raises(RuntimeError) as exc_info:
        getattr(BitableClient, method_name)(FailingClient(), "tbl-private", records)

    output = caplog.text + str(exc_info.value)
    assert "count=1" in output
    assert "start_index=0" in output
    assert "fields=['Address', 'Email']" in output
    assert "code=1254064" in output
    assert "message=<redacted>" in output
    assert PRIVATE_EMAIL not in output
    assert PRIVATE_ADDRESS not in output
    assert PRIVATE_TOKEN not in output
    assert "rec-private-identifier" not in output
    assert "tbl-private" not in output
