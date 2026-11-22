import os
import sys
import logging
import requests
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel
from ogx_client import OgxClient

# Structured logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ogx-guardrails-app")

app = FastAPI(
    title="OpenShift AI OGX & Guardrails Demo Service",
    description="Microservice demonstrating OpenShift AI 3.5 OGX server consumption integrated with NeMo Guardrails safety checks.",
    version="1.0.0"
)

# 1. Internal OpenShift Service Resolution
OGX_URL = os.getenv("OGX_URL", "http://ogx-service:8321")
GUARDRAILS_URL = os.getenv("GUARDRAILS_URL", "http://nemo-checks-demo:8000")
SERVICEACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

def get_auth_token() -> str:
    """Retrieve auth token from mounted ServiceAccount secret or environment variable."""
    if os.path.exists(SERVICEACCOUNT_TOKEN_PATH):
        try:
            with open(SERVICEACCOUNT_TOKEN_PATH, "r") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read ServiceAccount token: {e}")
    return os.getenv("OPENSHIFT_AUTH_TOKEN", "")

# 2. Initialize OGX Client (connects at service root without /v1)
ogx_client = OgxClient(base_url=OGX_URL)

class ChatRequest(BaseModel):
    prompt: str

class ChatResponse(BaseModel):
    allowed: bool
    response: str
    blocked_reason: str = ""

def check_guardrails(prompt: str) -> tuple[bool, str]:
    """
    Validate user prompt against NeMo Guardrails /v1/guardrail/checks endpoint.
    Follows Fail-Closed security policy: timeouts or errors immediately block execution.
    """
    token = get_auth_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    payload = {
        "model": "guardrails-evaluator",
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        res = requests.post(
            f"{GUARDRAILS_URL}/v1/guardrail/checks",
            headers=headers,
            json=payload,
            timeout=5.0
        )
        
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "blocked":
                activated_rails = data.get("guardrails_data", {}).get("log", {}).get("activated_rails", [])
                reason = f"Security rails triggered: {', '.join(activated_rails)}"
                logger.warning(f"Prompt blocked by guardrail: {reason}")
                return False, reason
            return True, ""
        else:
            logger.error(f"HTTP error from Guardrails: {res.status_code} - {res.text}")
            return False, "Moderation service returned error status (Fail-closed)."
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Guardrails communication timeout or failure: {e}")
        return False, "Moderation service currently unreachable."

@app.get("/healthz", status_code=status.HTTP_200_OK)
def liveness():
    """Liveness probe for OpenShift."""
    return {"status": "alive"}

@app.get("/ready", status_code=status.HTTP_200_OK)
def readiness():
    """Readiness probe validating baseline connection to OGX."""
    try:
        models = ogx_client.models.list()
        return {"status": "ready", "models_available": len(models)}
    except Exception as e:
        logger.warning(f"Readiness probe failed connecting to OGX: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

@app.post("/chat", response_model=ChatResponse)
def generate_chat(req: ChatRequest):
    logger.info(f"Incoming prompt: {req.prompt[:60]}...")
    
    # Step 1: Input Guardrail Validation
    allowed, reason = check_guardrails(req.prompt)
    if not allowed:
        return ChatResponse(
            allowed=False,
            response="Sua solicitação foi bloqueada pelas políticas de segurança corporativa.",
            blocked_reason=reason
        )

    # Step 2: Inference via OGX
    try:
        models = ogx_client.models.list()
        model_id = next(m.identifier for m in models if m.model_type == "llm")

        completion = ogx_client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": "Você é um assistente técnico especializado em Red Hat OpenShift AI."},
                {"role": "user", "content": req.prompt}
            ]
        )
        output = completion.choices.message.content
        return ChatResponse(allowed=True, response=output)

    except Exception as e:
        logger.error(f"Inference error on OGX: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, log_level="info")
