from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from strawberry.fastapi import GraphQLRouter
from strawberry.subscriptions import (
    GRAPHQL_TRANSPORT_WS_PROTOCOL,
    GRAPHQL_WS_PROTOCOL,
)

from app.graphql.schema import schema
from app.graphql.context import build_context
from app.webhooks.whatsapp_webhook import router as whatsapp_webhook_router
from app.services.whatsapp_listener import listener as whatsapp_listener
from app.services.notification_scheduler import scheduler as notification_scheduler

# Initialize logging system
from app.core.logging_config import setup_logging, get_logger

# Initialize logging first
logger = setup_logging()
logger.info("Starting FitPilot backend application")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the Postgres LISTEN/NOTIFY bridge for WhatsApp realtime events.
    await whatsapp_listener.start()
    logger.info("WhatsApp NOTIFY listener started")
    # Start the daily notification sweep (renewal reminders + expired win-back).
    notification_scheduler.start()
    try:
        yield
    finally:
        notification_scheduler.stop()
        await whatsapp_listener.stop()
        logger.info("WhatsApp NOTIFY listener stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok"}


# Mount static files for profile pictures
uploads_path = Path(__file__).parent.parent / "uploads"
uploads_path.mkdir(exist_ok=True)  # Ensure uploads directory exists
app.mount("/uploads", StaticFiles(directory=str(uploads_path)), name="uploads")
logger.info(f"Static files mounted at /uploads from {uploads_path}")

# Basic request logging middleware (helps trace login attempts)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    req_logger = get_logger("requests")
    response = await call_next(request)
    try:
        req_logger.info(f"{request.method} {request.url.path} -> {response.status_code}")
    except Exception:
        pass
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8080"],  # Específico para desarrollo
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

graphql_app = GraphQLRouter(
    schema=schema,
    context_getter=build_context,
    graphql_ide="graphiql",
    subscription_protocols=[
        GRAPHQL_TRANSPORT_WS_PROTOCOL,
        GRAPHQL_WS_PROTOCOL,
    ],
    multipart_uploads_enabled=True,
)
app.include_router(graphql_app, prefix="/graphql")

# Inbound WhatsApp Cloud API webhook (FitPilot owns ingestion)
app.include_router(whatsapp_webhook_router)

