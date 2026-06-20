import json
import hashlib
import numpy as np
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.config import settings
from app.models import Memory

# Initialize Gemini Client if API key is provided
client = None
if settings.GEMINI_API_KEY:
    try:
        from google import genai
        # Initialize client. The SDK automatically picks up GEMINI_API_KEY from environment
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        print(f"Failed to initialize Gemini Client: {e}")

def get_embedding(text: str) -> list[float]:
    """
    Generates a 768-dimensional embedding for a given text.
    If GEMINI_API_KEY is available, uses the Gemini text-embedding-004 model.
    Otherwise, generates a deterministic mock vector using SHA-256 hashing.
    """
    if not text:
        return [0.0] * 768

    if client:
        try:
            response = client.models.embed_content(
                model="text-embedding-004",
                contents=text
            )
            # The structure of the response usually contains 'embeddings' list
            # We access the first embedding's values
            if response and hasattr(response, 'embeddings') and response.embeddings:
                return response.embeddings[0].values
            elif response and hasattr(response, 'embedding') and response.embedding:
                return response.embedding.values
        except Exception as e:
            print(f"Error generating embedding via Gemini API: {e}. Falling back to mock embedding.")

    # Mock embedding: Deterministic unit vector of size 768 based on SHA-256 hash of the text
    # This guarantees that identical texts have exactly 1.0 similarity (cosine similarity)
    hasher = hashlib.sha256(text.encode('utf-8'))
    seed = int(hasher.hexdigest()[:8], 16)
    
    # Seed numpy locally to make this thread-safe and deterministic
    rng = np.random.default_rng(seed)
    mock_vector = rng.normal(size=768)
    
    # Normalize to unit vector
    norm = np.linalg.norm(mock_vector)
    if norm > 0:
        mock_vector = mock_vector / norm
        
    return mock_vector.tolist()

def store_memory(db: Session, project_id: str, content: str, task_id: str = None) -> Memory:
    """
    Computes embedding and stores content in memories table.
    """
    embedding = get_embedding(content)
    memory = Memory(
        project_id=project_id,
        task_id=task_id,
        content=content,
        embedding=embedding
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory

def retrieve_memories(db: Session, project_id: str, query_text: str, limit: int = 3) -> list[tuple[Memory, float]]:
    """
    Generates embedding for query_text and finds the top `limit` most semantically
    similar memories associated with project_id.
    Returns list of tuples: (Memory object, cosine_similarity).

    Uses pgvector's cosine_distance operator when available (PostgreSQL).
    Falls back to pure-Python numpy cosine similarity ranking for SQLite/local dev.
    """
    query_embedding = get_embedding(query_text)
    query_vec = np.array(query_embedding)
    query_norm = np.linalg.norm(query_vec)

    # --- Try pgvector native ordering first ---
    try:
        results = (
            db.query(Memory)
            .filter(Memory.project_id == project_id)
            .order_by(Memory.embedding.cosine_distance(query_embedding))
            .limit(limit)
            .all()
        )
    except Exception:
        # pgvector not available (e.g. SQLite local dev mode):
        # fetch all memories and rank in Python
        all_mems = (
            db.query(Memory)
            .filter(Memory.project_id == project_id)
            .all()
        )
        if not all_mems:
            return []

        scored = []
        for mem in all_mems:
            try:
                a = np.array(mem.embedding if isinstance(mem.embedding, list)
                             else json.loads(mem.embedding))
                norm_a = np.linalg.norm(a)
                if norm_a > 0 and query_norm > 0:
                    sim = float(np.dot(a, query_vec) / (norm_a * query_norm))
                else:
                    sim = 0.0
            except Exception:
                sim = 0.0
            scored.append((mem, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # Compute similarity scores for pgvector results
    memories_with_scores = []
    for mem in results:
        try:
            a = np.array(mem.embedding if isinstance(mem.embedding, list)
                         else json.loads(mem.embedding))
            norm_a = np.linalg.norm(a)
            if norm_a > 0 and query_norm > 0:
                sim = float(np.dot(a, query_vec) / (norm_a * query_norm))
            else:
                sim = 0.0
        except Exception:
            sim = 0.0
        memories_with_scores.append((mem, sim))

    return memories_with_scores
