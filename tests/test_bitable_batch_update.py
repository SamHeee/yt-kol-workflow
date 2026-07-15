from feishu.bitable import BitableClient


def test_batch_update_uses_post_endpoint():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def _post(self, path, data):
            self.calls.append((path, data))
            return {
                "code": 0,
                "data": {"records": data["records"]},
            }

    client = FakeClient()
    records = [{"record_id": "rec1", "fields": {"KOL Name": "Sarah"}}]

    updated = BitableClient.batch_update_records(client, "tbl1", records)

    assert updated == 1
    assert client.calls == [
        ("/tables/tbl1/records/batch_update", {"records": records})
    ]
