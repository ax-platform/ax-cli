import importlib.util
from pathlib import Path


def _load_demo_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run-widget-demo.py"
    spec = importlib.util.spec_from_file_location("run_widget_demo", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self):
        self.messages = []

    def send_message(self, space_id, content, *, metadata=None, message_type="text", **kwargs):
        self.messages.append(
            {
                "space_id": space_id,
                "content": content,
                "metadata": metadata,
                "message_type": message_type,
                "kwargs": kwargs,
            }
        )
        return {"id": "msg-space-draft"}


def test_demo_space_draft_card_opens_review_widget():
    runner = _load_demo_runner()
    fake = FakeClient()

    message_id, draft_name = runner.send_space_draft(fake, "space-1")

    assert message_id == "msg-space-draft"
    assert draft_name.startswith("demo_workspace_")
    assert len(fake.messages) == 1

    metadata = fake.messages[0]["metadata"]
    widget = metadata["ui"]["widget"]
    assert widget["tool_name"] == "spaces"
    assert widget["tool_action"] == "create_draft"
    assert widget["resource_uri"] == "ui://spaces/navigator"

    initial_data = widget["initial_data"]
    assert initial_data["kind"] == "space_collection"
    assert initial_data["state"] == "approval_required"
    assert initial_data["data"]["scope"] == "create"
    assert initial_data["data"]["draft"]["name"] == draft_name
    assert initial_data["data"]["count"] == 1
    assert initial_data["data"]["items"][0]["name"] == draft_name

    card = metadata["ui"]["cards"][0]
    assert card["type"] == "confirmation"
    assert card["payload"]["intent"] == "review"
    assert card["payload"]["resource_type"] == "space_draft"
