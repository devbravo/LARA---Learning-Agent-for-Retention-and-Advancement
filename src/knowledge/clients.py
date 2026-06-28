"""Shared client container for the knowledge-graph feature.

Instantiate ``KnowledgeClients`` once per pipeline invocation and pass it
into pipeline functions as an argument — don't reconstruct connections per call.

Business logic (search, extract, synthesize, resolve, write) lives in plain
functions that accept ``clients: KnowledgeClients`` as a parameter.
"""

import os
import sqlite3
from pathlib import Path

import anthropic
import voyageai
from dotenv import load_dotenv
from neo4j import GraphDatabase, Driver

from src.infrastructure.db import get_connection

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

VOYAGE_MODEL = "voyage-4-lite"


class KnowledgeClients:
    """Shared clients for the knowledge-graph feature. Instantiate once,
    pass into pipeline functions as an argument — don't reconstruct
    connections per call.

    Attributes:
        anthropic: Anthropic API client for Claude calls.
        voyage: Voyage AI client; use with ``VOYAGE_MODEL`` for embeddings.
        db: SQLite connection via ``get_connection()`` (row_factory set).
        neo4j: Neo4j driver connected to the AuraDB instance.
        neo4j_database: Target Neo4j database name from ``NEO4J_DATABASE``.

    Raises:
        EnvironmentError: If any required env var is missing.
    """

    def __init__(self) -> None:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise EnvironmentError("Missing required env var: ANTHROPIC_API_KEY")
        self.anthropic: anthropic.Anthropic = anthropic.Anthropic(api_key=anthropic_key)

        voyage_key = os.environ.get("VOYAGE_API_KEY")
        if not voyage_key:
            raise EnvironmentError("Missing required env var: VOYAGE_API_KEY")
        self.voyage: voyageai.Client = voyageai.Client(api_key=voyage_key)

        self.db: sqlite3.Connection = get_connection()

        neo4j_uri = os.environ.get("NEO4J_URI")
        neo4j_user = os.environ.get("NEO4J_USERNAME")
        neo4j_pass = os.environ.get("NEO4J_PASSWORD")
        neo4j_db = os.environ.get("NEO4J_DATABASE")
        if not all([neo4j_uri, neo4j_user, neo4j_pass, neo4j_db]):
            raise EnvironmentError(
                "Missing one or more required env vars: "
                "NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE"
            )
        self.neo4j: Driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
        self.neo4j_database: str = neo4j_db

    def close(self) -> None:
        """Release long-lived connections. Call when the pipeline is done.

        Args: None
        Returns: None
        """
        self.db.close()
        self.neo4j.close()
