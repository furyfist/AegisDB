"""
ChromaDB wrapper for storing engineer rejection reasons.
Separate collection from aegisdb_fixes — rejections carry different
signal (what NOT to do and why) vs fixes (what works).

Used by:
  - app.py handle_rejection_submit() → store_rejection()
  - qa_engine.py → find_rejections_for_table()

ChromaDB client is synchronous — all calls run in thread executor.
"""

import uuid
import logging
from datetime import datetime
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

REJECTION_COLLECTION = "aegisdb_rejections"


class RejectionStore:

    def __init__(self, persist_dir: str = "./data/chromadb"):
        self._client = None
        self._collection = None
        self._embed_fn = None
        self._persist_dir = persist_dir

    def connect(self):
        """
        Synchronous connect — same pattern as VectorStore.connect().
        Uses the SAME chromadb PersistentClient path so both collections
        live in the same on-disk store.
        """
        self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"  # same model as aegisdb_fixes
        )
        self._client = chromadb.PersistentClient(path=self._persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=REJECTION_COLLECTION,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        count = self._collection.count()
        logger.info(
            f"[RejectionStore] Connected — '{REJECTION_COLLECTION}' "
            f"has {count} documents"
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
        """
        Embed and store a rejection so future diagnosis prompts can
        reference it via find_rejections_for_table().

        Document text mirrors VectorStore._build_document() pattern
        so similarity search works across both collections.
        """
        rejection_id = str(uuid.uuid4())
        categories_str = ", ".join(failure_categories) or "unknown"

        # What gets embedded — problem + rejected fix + reason
        # Phrased so similarity search on future problems retrieves this
        doc = (
            f"Table: {table_fqn}\n"
            f"Failure: {categories_str}\n"
            f"Rejected fix: {fix_description}\n"
            f"Fix SQL: {fix_sql}\n"
            f"Rejection reason: {rejection_reason}\n"
            f"Alternative suggested: {alternative or 'none'}"
        )

        metadata = {
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
        }

        self._collection.add(
            ids=[rejection_id],
            documents=[doc],
            metadatas=[metadata],
        )
        logger.info(
            f"[RejectionStore] Stored rejection {rejection_id} "
            f"for {table_fqn} | reason='{rejection_reason[:60]}...'"
        )
        return rejection_id

    def find_rejections_for_table(
        self,
        table_fqn: str,
        failure_category: str = "",
        top_k: int = 3,
    ) -> list[dict]:
        """
        Retrieve past rejections relevant to this table + failure type.
        Returns raw metadata dicts — QA engine formats them into prompt text.
        """
        if not self._collection or self._collection.count() == 0:
            return []

        query = (
            f"Table: {table_fqn}\n"
            f"Failure: {failure_category}"
        )

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, self._collection.count()),
                include=["metadatas", "distances"],
            )
        except Exception as e:
            logger.error(f"[RejectionStore] Query failed: {e}")
            return []

        rejections = []
        for meta, distance in zip(
            results["metadatas"][0],
            results["distances"][0],
        ):
            similarity = round(1.0 - (distance / 2.0), 4)
            if similarity < 0.25:   # lower threshold than fixes — rejections are sparse
                continue
            rejections.append({**meta, "similarity": similarity})

        logger.info(
            f"[RejectionStore] Found {len(rejections)} rejections "
            f"for {table_fqn}"
        )
        return rejections

    def count(self) -> int:
        if not self._collection:
            return 0
        return self._collection.count()


# Module singleton — connected lazily in app.py boot
rejection_store = RejectionStore()