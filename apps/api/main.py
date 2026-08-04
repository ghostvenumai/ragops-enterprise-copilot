"""ASGI entrypoint for RAGOps Enterprise Copilot."""

from ragops.api.app import create_app

app = create_app()
