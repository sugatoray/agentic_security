import os
import random
from asyncio import Event, Queue
from datetime import datetime
from logging import config
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .http_spec import LLMSpec
from .probe_actor import fuzzer
from .probe_actor.refusal import REFUSAL_MARKS
from .probe_data import REGISTRY
from .report_chart import plot_security_report

# Create the FastAPI app instance
app = FastAPI()
origins = [
    "*",
]


# Configuration
class Settings:
    MAX_BUDGET = 1000
    MAX_DATASETS = 10
    RATE_LIMIT = "100/minute"
    DISABLE_TELEMETRY = os.getenv("DISABLE_TELEMETRY", False)
    FEATURE_PROXY = False


settings = Settings()

# Middleware setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

tools_inbox = Queue()
# Global stop event for cancelling scans
stop_event = Event()  # Added stop_event to cancel the scan


@app.get("/")
async def root():
    agentic_security_path = Path(__file__).parent
    return FileResponse(f"{agentic_security_path}/static/index.html")


@app.get("/main.js")
async def main_js():
    agentic_security_path = Path(__file__).parent
    return FileResponse(f"{agentic_security_path}/static/main.js")


@app.get("/telemetry.js")
async def telemetry_js():
    agentic_security_path = Path(__file__).parent
    if settings.DISABLE_TELEMETRY:
        return FileResponse(f"{agentic_security_path}/static/telemetry_disabled.js")
    return FileResponse(f"{agentic_security_path}/static/telemetry.js")


@app.get("/favicon.ico")
async def favicon():
    agentic_security_path = Path(__file__).parent
    return FileResponse(f"{agentic_security_path}/static/favicon.ico")


class LLMInfo(BaseModel):
    spec: str


@app.post("/verify")
async def verify(info: LLMInfo):

    spec = LLMSpec.from_string(info.spec)
    r = await spec.probe("test")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return dict(
        status_code=r.status_code,
        body=r.text,
        elapsed=r.elapsed.total_seconds(),
        timestamp=datetime.now().isoformat(),
    )


class Scan(BaseModel):
    llmSpec: str
    maxBudget: int
    datasets: list[dict] = []
    optimize: bool = False


class ScanResult(BaseModel):
    module: str
    tokens: int
    cost: float
    progress: float
    failureRate: float = 0.0


def streaming_response_generator(scan_parameters: Scan):
    # The generator function for StreamingResponse
    request_factory = LLMSpec.from_string(scan_parameters.llmSpec)

    async def _gen():
        async for scan_result in fuzzer.perform_scan(
            request_factory=request_factory,
            max_budget=scan_parameters.maxBudget,
            datasets=scan_parameters.datasets,
            tools_inbox=tools_inbox,
            optimize=scan_parameters.optimize,
            stop_event=stop_event,  # Pass the stop_event to the generator
        ):
            yield scan_result + "\n"  # Adding a newline for separation

    return _gen()


@app.post("/scan")
async def scan(scan_parameters: Scan, background_tasks: BackgroundTasks):

    # Initiates streaming of scan results
    return StreamingResponse(
        streaming_response_generator(scan_parameters), media_type="application/json"
    )


class Probe(BaseModel):
    prompt: str


@app.post("/v1/self-probe")
def self_probe(probe: Probe):
    refuse = random.random() < 0.2
    message = random.choice(REFUSAL_MARKS) if refuse else "This is a test!"
    message = probe.prompt + " " + message
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1677858242,
        "model": "gpt-3.5-turbo-0613",
        "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
        "choices": [
            {
                "message": {"role": "assistant", "content": message},
                "logprobs": None,
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    }


@app.get("/v1/data-config")
async def data_config():
    return [m for m in REGISTRY]


@app.get("/failures")
async def failures_csv():
    if not Path("failures.csv").exists():
        return {"error": "No failures found"}
    return FileResponse("failures.csv")


class Table(BaseModel):
    table: list[dict]


@app.post("/plot.jpeg", response_class=Response)
async def get_plot(table: Table):
    buf = plot_security_report(table.table)
    return StreamingResponse(buf, media_type="image/jpeg")


class Message(BaseModel):
    role: str
    content: str


class CompletionRequest(BaseModel):
    """Model for completion requests."""

    model: str
    messages: list[Message]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    n: int = Field(default=1, ge=1, le=10)
    stop: list[str] | None = None
    max_tokens: int = Field(default=100, ge=1, le=4096)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)


# OpenAI proxy endpoint
@app.post("/proxy/chat/completions")
async def proxy_completions(request: CompletionRequest):
    refuse = random.random() < 0.2
    message = random.choice(REFUSAL_MARKS) if refuse else "This is a test!"
    prompt_content = " ".join(
        [msg.content for msg in request.messages if msg.role == "user"]
    )
    message = prompt_content + " " + message
    ready = Event()
    ref = dict(message=message, reply="", ready=ready)
    tools_inbox.put_nowait(ref)
    if settings.FEATURE_PROXY:
        # Proxy to agent
        await ready.wait()
        reply = ref["reply"]
        return reply
    # Simulate a completion response
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1677858242,
        "model": "gpt-3.5-turbo-0613",
        "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
        "choices": [
            {
                "message": {"role": "assistant", "content": message},
                "logprobs": None,
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    }


config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": True,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
            },
        },
        "root": {
            "handlers": ["console"],
            "level": "INFO",
        },
        "loggers": {
            "uvicorn.access": {
                "level": "ERROR",  # Set higher log level to suppress info logs globally
                "handlers": ["console"],
                "propagate": False,
            }
        },
    }
)


@app.post("/stop")
async def stop_scan():
    stop_event.set()  # Set the stop event to cancel the scan
    return {"status": "Scan stopped"}


class LogNon200ResponsesMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as e:
            logger.exception("Yikes")
            raise e
        if response.status_code != 200:
            logger.error(
                f"{request.method} {request.url} - Status code: {response.status_code}"
            )
        return response


# Add middleware to the application
app.add_middleware(LogNon200ResponsesMiddleware)
