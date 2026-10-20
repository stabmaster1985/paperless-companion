"""Client für einen lokalen Ollama-Server."""

import json

import httpx


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        keep_alive: str = "2m",
        num_ctx: int = 4096,
        timeout: float = 900.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def status(self) -> dict:
        """Erreichbarkeit und ob das Modell bereits heruntergeladen ist."""
        try:
            response = self._client.get("/api/tags", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return {"reachable": False, "model_present": False, "error": str(exc)}
        names = [m.get("name", "") for m in response.json().get("models", [])]
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return {"reachable": True, "model_present": wanted in names, "models": names}

    def extract_json(self, system: str, prompt: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        try:
            response = self._client.post("/api/chat", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama-Anfrage fehlgeschlagen: {exc}") from exc
        content = response.json().get("message", {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ungültiges JSON vom Modell: {content[:200]}") from exc
        if not isinstance(data, dict):
            raise OllamaError("Modellantwort ist kein JSON-Objekt")
        return data
