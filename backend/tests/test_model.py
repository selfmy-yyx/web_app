import io
import json

from backend.model import OpenAICompatibleModel


def test_adapter_uses_backend_environment_and_returns_the_public_json_content(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://model.example/v1/")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("MODEL_API_KEY", "server-test-secret")
    expected = {"text": "请各位发表观点。"}
    requests = []

    def respond(request, timeout):
        requests.append(request)
        assert timeout == 60
        payload = {"choices": [{"message": {
            "content": json.dumps(expected),
            "reasoning_content": "provider-private-reasoning",
        }}]}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("backend.model.urlopen", respond)
    model = OpenAICompatibleModel.from_env()

    assert model("utterance", topic="混合办公") == expected
    request, = requests
    assert request.full_url == "https://model.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer server-test-secret"
    assert json.loads(request.data)["model"] == "test-model"
    assert "server-test-secret" not in repr(model)
