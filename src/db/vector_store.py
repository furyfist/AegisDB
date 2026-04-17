import json
import uuid
import logging
import chromadb
from chromadb.utils import embedding_functions
from src.core.config import settings
from src.core.models import SimilarFix, DiagnosisResult, FailureCategory

logger = logging.getLogger(__name__)


class VectorStore:
    """
    ChromaDB wrapper for storing and retrieving past fixes.
    Uses sentence-transformers for local embeddings — no API calls.
    """

    def __init__(self):
        self._client: chromadb.ClientAPI | None = None
        self._collection = None
        self._embed_fn = None

    def connect(self):
        """Synchronous — ChromaDB client is not async."""
        self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"  # fast, 384-dim, good enough for fixes
        )
        self._client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir
        )
        self._collection = self._client.get_or_create_collection(
            name=settings.chroma_collection,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        count = self._collection.count()
        logger.info(f"ChromaDB connected — collection '{settings.chroma_collection}' "
                    f"has {count} documents")

    def _build_document(
        self,
        table_fqn: str,
        failure_category: str,
        problem_description: str,
        fix_sql: str,
    ) -> str:
        """
        What gets embedded. Include both problem + fix so similarity
        search finds relevant fixes even when phrasing differs.
        """
        return (
            f"Table: {table_fqn}\n"
            f"Failure: {failure_category}\n"
            f"Problem: {problem_description}\n"
            f"Fix: {fix_sql}"
        )

    def store_fix(
        self,
        table_fqn: str,
        failure_category: str,
        problem_description: str,
        fix_sql: str,
        was_successful: bool,
        diagnosis_result: DiagnosisResult,
    ) -> str:
        """Store a fix after it was applied. Called by Phase 5 feedback loop."""
        fix_id = str(uuid.uuid4())
        doc = self._build_document(
            table_fqn, failure_category, problem_description, fix_sql
        )
        self._collection.add(
            ids=[fix_id],
            documents=[doc],
            metadatas=[{
                "fix_id": fix_id,
                "table_fqn": table_fqn,
                "failure_category": failure_category,
                "problem_description": problem_description,
                "fix_sql": fix_sql,
                "was_successful": str(was_successful),
                "event_id": diagnosis_result.event_id,
                "stored_at": diagnosis_result.diagnosed_at.isoformat(),
            }],
        )
        logger.info(f"Stored fix {fix_id} for {table_fqn} [{failure_category}]")
        return fix_id

    def find_similar_fixes(
        self,
        table_fqn: str,
        failure_category: str,
        problem_description: str,
        top_k: int = 3,
    ) -> list[SimilarFix]:
        """
        Retrieve top-k most similar past fixes.
        Returns empty list if collection is empty — first run has no history.
        """
        if self._collection.count() == 0:
            logger.info("Knowledge base empty — no similar fixes to retrieve")
            return []

        query_doc = self._build_document(
            table_fqn, failure_category, problem_description, ""
        )

        results = self._collection.query(
            query_texts=[query_doc],
            n_results=min(top_k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        fixes = []
        for meta, distance in zip(
            results["metadatas"][0],
            results["distances"][0],
        ):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score: 1 - (distance/2)
            similarity = round(1.0 - (distance / 2.0), 4)
            if similarity < 0.3:
                continue  # too dissimilar, skip

            fixes.append(SimilarFix(
                fix_id=meta.get("fix_id", f"unknown_{i}"),
                table_fqn=meta.get("table_fqn", ""),
                failure_category=meta.get("failure_category", ""),
                problem_description=meta.get("problem_description", ""),
                fix_sql=meta.get("fix_sql", ""),
                was_successful=meta.get("was_successful", "False") == "True",
                similarity_score=similarity,
            ))

        logger.info(f"Found {len(fixes)} similar fixes for [{failure_category}]")
        return fixes

    def seed_bootstrap_fixes(self):
        """
        Seed the KB with common fixes so the agent isn't blind on first run.
        Call this once on startup if collection is empty.
        """
        if self._collection.count() > 0:
            return

        logger.info("Seeding bootstrap fixes into knowledge base...")
        bootstrap = [
            {
                "fix_id": "bootstrap_0",
                "table_fqn": "*.*.public.orders",
                "failure_category": "null_violation",
                "problem_description": "NULL values found in customer_id column which should be NOT NULL",
                "fix_sql": "DELETE FROM {table} WHERE customer_id IS NULL;",
                "was_successful": "True",
            },
            {
                "fix_id": "bootstrap_1",
                "table_fqn": "*.*.public.orders",
                "failure_category": "range_violation",
                "problem_description": "Negative amount values found in orders table",
                "fix_sql": "UPDATE {table} SET amount = ABS(amount) WHERE amount < 0;",
                "was_successful": "True",
            },
            {
                "fix_id": "bootstrap_2",
                "table_fqn": "*.*.public.customers",
                "failure_category": "uniqueness_violation",
                "problem_description": "Duplicate email values found in customers table",
                "fix_sql": (
                    "DELETE FROM {table} WHERE ctid NOT IN ("
                    "SELECT MIN(ctid) FROM {table} GROUP BY email);"
                ),
                "was_successful": "True",
            },
            {
                "fix_id": "bootstrap_3",
                "table_fqn": "*.*.public.customers",
                "failure_category": "range_violation",
                "problem_description": "Invalid age values found — negative or over 150",
                "fix_sql": "DELETE FROM {table} WHERE age < 0 OR age > 150;",
                "was_successful": "True",
            },
            {
                "fix_id": "bootstrap_4",
                "table_fqn": "*.*.public.orders",
                "failure_category": "null_violation",
                "problem_description": "NULL status values in orders table",
                "fix_sql": "UPDATE {table} SET status = 'unknown' WHERE status IS NULL;",
                "was_successful": "True",
            },
        ]

        for i, fix in enumerate(bootstrap):
            doc = self._build_document(
                fix["table_fqn"],
                fix["failure_category"],
                fix["problem_description"],
                fix["fix_sql"],
            )
            self._collection.add(
                ids=[f"bootstrap_{i}"],
                documents=[doc],
                metadatas=[fix],
            )
        logger.info(f"Seeded {len(bootstrap)} bootstrap fixes")


# Module-level singleton
vector_store = VectorStore()