"""
ChromaDB wrapper for storing engineer rejection reasons.
Separate collection from aegisdb_fixes — rejections encode
what NOT to do and why, not what works.

All methods are synchronous (ChromaDB client is not async).
Call via loop.run_in_executor() from async handlers.
"""

import uuid
import logging
from datetime import datetime

import chromadb
from chromadb.utils import embedding_functions

from slack_bot.config import slack_settings

logger = logging.getLogger(__name__)

REJECTION_COLLECTION = "aegisdb_rejections"


class RejectionStore:

    def __init__(self):
        self._client = None
        self._collection = None
        self._embed_fn = None

    def connect(self):
        """
        Synchronous connect. Same PersistentClient path as the backend's
        VectorStore so both collections share one on-disk store.
        """
        self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self._client = chromadb.PersistentClient(
            path=slack_settings.chroma_persist_dir
        )
        self._collection = self._client.get_or_create_collection(
            name=REJECTION_COLLECTION,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"[RejectionStore] Connected — '{REJECTION_COLLECTION}' "
            f"has {self._collection.count()} documents"
        )

    def store_rejection(
        self,
        proposal_id: str,
        table_fqn: str,
        table_name: str,
        failure_categories: list[str],
        fix_sql: str,
        fix_description: str,
        rejection_reason: str,
        alternative: str,
        decided_by: str,
    ) -> str:
        """Store a rejection so future diagnosis prompts can learn from it."""
        if not self._collection:
            logger.warning("[RejectionStore] Not connected — skipping store")
            return ""

        rejection_id  = str(uuid.uuid4())
        categories_str = ", ".join(failure_categories) or "unknown"

        doc = (
            f"Table: {table_fqn}\n"
            f"Failure: {categories_str}\n"
            f"Rejected fix: {fix_description}\n"
            f"Fix SQL: {fix_sql}\n"
            f"Rejection reason: {rejection_reason}\n"
            f"Alternative suggested: {alternative or 'none'}"
        )

        self._collection.add(
            ids=[rejection_id],
            documents=[doc],
            metadatas=[{
                "rejection_id":       rejection_id,
                "proposal_id":        proposal_id,
                "table_fqn":          table_fqn,
                "table_name":         table_name,
                "failure_categories": categories_str,
                "fix_sql":            fix_sql,
                "rejection_reason":   rejection_reason,
                "alternative":        alternative or "",
                "decided_by":         decided_by,
                "rejected_at":        datetime.utcnow().isoformat(),
            }],
        )
        logger.info(
            f"[RejectionStore] Stored {rejection_id} for {table_fqn}"
        )
        return rejection_id

    def find_rejections_for_table(
        self,
        table_fqn: str,
        failure_category: str = "",
        top_k: int = 3,
    ) -> list[dict]:
        """
        Retrieve past rejections for this table + failure type.
        Safe to call before connect() — returns [] immediately.
        """
        if not self._collection:
            return []
        if self._collection.count() == 0:
            return []

        query = f"Table: {table_fqn}\nFailure: {failure_category}"

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, self._collection.count()),
                include=["metadatas", "distances"],
            )
        except Exception as e:
            logger.error(f"[RejectionStore] Query failed: {e}")
            return []

        out = []
        for meta, dist in zip(
            results["metadatas"][0],
            results["distances"][0],
        ):
            similarity = round(1.0 - (dist / 2.0), 4)
            if similarity < 0.25:
                continue
            out.append({**meta, "similarity": similarity})

        logger.info(
            f"[RejectionStore] {len(out)} rejections found for '{table_fqn}'"
        )
        return out

    def count(self) -> int:
        if not self._collection:
            return 0
        return self._collection.count()


rejection_store = RejectionStore()